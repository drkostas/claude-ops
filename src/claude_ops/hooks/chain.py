"""Run several tool hooks in one interpreter, in order (a hook chain).

Claude Code starts one process for every hook command that matches a tool call. With half a dozen
Python hooks on Bash, every Bash call starts half a dozen interpreters, each importing the same
modules, before the command runs. On a machine with many busy sessions that is a real share of the
CPU. The chain is one settings.json entry that reads the call once and runs the same hooks in one
interpreter:

- a `module` step imports a hook module and calls its `main` with the step's `args`, with the call on
  standard input, as Claude Code would run it. A module that exits 2 with a reason starting with
  "Blocked:" refuses the call: the chain stops there and refuses with that reason. A module whose
  JSON answer denies the call (`hookSpecificOutput.permissionDecision` "deny") stops it the same way.
  Anything else lets the call go on to the next step.
- a `command` step runs a program (a shell hook, for example) with the call on its standard input,
  after the steps before it allowed the call. Its exit 2 refuses only when its text starts with
  "Blocked:", because exit 2 is also how a broken interpreter fails. What it prints on standard
  output (an `updatedInput`, for example) is passed on.
- each step names the tools it applies to (`tools`, a regular expression matched against the whole
  tool name, as settings.json's `matcher` is) and, optionally, the hook events (`events`).

A command step finds the module steps that already ran and allowed the call in the environment
variable CLAUDE_OPS_CHAIN_PASSED (their module names, comma separated), so a shell hook that would
start one of them itself can skip it.

The file is `--config`, else `CLAUDE_OPS_HOOK_CHAIN_CONFIG`, else
`~/.config/claude-ops/hook-chain.json`:

    {"steps": [
      {"module": "claude_ops.hooks.no_session_links", "tools": "Bash|Write|Edit|MultiEdit|mcp__github__.*"},
      {"module": "claude_ops.hooks.plain_voice", "args": ["--config", "~/.config/claude-ops/plain-voice.json"],
       "tools": "Bash|mcp__github__.*"},
      {"command": ["~/.claude/hooks/rewrite.sh"], "tools": "Bash", "timeout": 10}
    ]}

A step that raises, a module that cannot be imported and a command that cannot start are skipped,
and with no readable config nothing is checked: a broken hook must never stop work.

    python3 -P -m claude_ops.hooks.chain [--config FILE]
"""
from __future__ import annotations

import contextlib
import importlib
import inspect
import io
import json
import os
import re
import subprocess
import sys

DEFAULT_CONFIG = "~/.config/claude-ops/hook-chain.json"
PASSED_ENV = "CLAUDE_OPS_CHAIN_PASSED"


def config(argv: list[str]) -> list[dict]:
    """The steps, from `--config`, the environment or the default file; [] when none is readable."""
    path = None
    for i, a in enumerate(argv):
        if a == "--config" and i + 1 < len(argv):
            path = argv[i + 1]
    path = path or os.environ.get("CLAUDE_OPS_HOOK_CHAIN_CONFIG") or DEFAULT_CONFIG
    try:
        with open(os.path.expanduser(path)) as f:
            steps = json.load(f).get("steps")
    except (OSError, ValueError, AttributeError):
        return []
    return [s for s in steps if isinstance(s, dict)] if isinstance(steps, list) else []


def applies(step: dict, tool: str, event: str) -> bool:
    try:
        if not re.fullmatch(step.get("tools") or ".*", tool or ""):
            return False
    except re.error:
        return False
    events = step.get("events")
    return not events or event in events


def _code(e: SystemExit) -> int:
    return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)


def run_module(name: str, args: list[str], raw: str) -> tuple[int, str, str] | None:
    """(exit code, standard output, standard error) of the module's main on this call; None when it broke."""
    saved = sys.stdin, sys.argv
    out, err = io.StringIO(), io.StringIO()
    try:
        mod = importlib.import_module(name)
        sys.stdin, sys.argv = io.StringIO(raw), [name, *args]
        takes_argv = bool(inspect.signature(mod.main).parameters)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = mod.main(list(args)) if takes_argv else mod.main()
            except SystemExit as e:
                rc = _code(e)
        return (rc if isinstance(rc, int) else 0), out.getvalue(), err.getvalue()
    except Exception:  # noqa: BLE001 - a broken hook must never stop work
        return None
    finally:
        sys.stdin, sys.argv = saved


def run_command(step: dict, raw: str, passed: list[str]) -> tuple[int, str, str] | None:
    cmd = step.get("command")
    argv = [cmd] if isinstance(cmd, str) else list(cmd or [])
    if not argv:
        return None
    env = {**os.environ, PASSED_ENV: ",".join(passed), **{str(k): str(v) for k, v in (step.get("env") or {}).items()}}
    try:
        p = subprocess.run([os.path.expanduser(a) for a in argv], input=raw, capture_output=True, text=True,
                           env=env, timeout=float(step.get("timeout") or 30))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return p.returncode, p.stdout, p.stderr


def _answer(text: str) -> dict | None:
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _denies(obj: dict) -> bool:
    out = obj.get("hookSpecificOutput")
    return isinstance(out, dict) and out.get("permissionDecision") == "deny"


def merge(answers: list[dict]) -> dict:
    """One answer from several: later keys win, and additionalContext texts are joined."""
    merged: dict = {}
    for a in answers:
        for k, v in a.items():
            if k == "hookSpecificOutput" and isinstance(v, dict):
                spec = merged.setdefault(k, {})
                for sk, sv in v.items():
                    if sk == "additionalContext" and spec.get(sk):
                        spec[sk] = f"{spec[sk]}\n\n{sv}"
                    else:
                        spec[sk] = sv
            else:
                merged[k] = v
    return merged


def chain(raw: str, steps: list[dict]) -> tuple[int, str, str]:
    """(exit code, standard output, standard error) for the call `raw` through `steps`."""
    try:
        payload = json.loads(raw)
    except ValueError:
        return 0, "", ""
    if not isinstance(payload, dict):
        return 0, "", ""
    tool, event = str(payload.get("tool_name") or ""), str(payload.get("hook_event_name") or "")
    passed: list[str] = []
    answers: list[dict] = []
    plain: list[str] = []
    for step in steps:
        if not applies(step, tool, event):
            continue
        if step.get("module"):
            got = run_module(str(step["module"]), [os.path.expanduser(str(a)) for a in step.get("args") or []], raw)
        else:
            got = run_command(step, raw, passed)
        if got is None:
            continue
        rc, out, err = got
        if rc == 2 and err.lstrip().startswith("Blocked:"):
            return 2, "", err
        ans = _answer(out.strip()) if out.strip() else None
        if ans is not None and _denies(ans):
            return 0, json.dumps(ans), ""
        if ans is not None:
            answers.append(ans)
        elif out.strip():
            plain.append(out.strip())
        if step.get("module") and rc == 0:
            passed.append(str(step["module"]))
    if answers:
        return 0, json.dumps(merge(answers)), ""
    return 0, "\n".join(plain), ""


def main(argv: list[str] | None = None) -> int:
    try:
        steps = config(list(sys.argv[1:] if argv is None else argv))
        rc, out, err = chain(sys.stdin.read(), steps)
    except Exception:  # noqa: BLE001 - a broken hook must never stop work
        return 0
    if out:
        print(out)
    if err:
        print(err, end="" if err.endswith("\n") else "\n", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())

"""claude-ops command line."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from importlib import resources
from pathlib import Path

from . import __version__, chat, inject, restore, sessions, slots

SKILLS = ("claude-ops", "chat-watcher", "mentor", "rover")

HOOKS = {
    "Stop": [{"hooks": [{"type": "command", "command": "claude-ops hook ask-what-you-need"}]}],
    "PostToolUse": [{"hooks": [{"type": "command", "command": "claude-ops hook context-check"}]}],
    "PostCompact": [{"hooks": [{"type": "command", "command": "claude-ops hook context-mark"}]}],
    "PreToolUse": [{"matcher": "Bash|Write|Edit|MultiEdit|mcp__github__.*",
                    "hooks": [{"type": "command", "command": "claude-ops hook no-session-links"}]},
                   {"matcher": "Bash|Write|Edit|MultiEdit|mcp__github__.*",
                    "hooks": [{"type": "command", "command": "claude-ops hook public-voice"}]},
                   {"matcher": "Bash|Write|Edit|MultiEdit|NotebookEdit",
                    "hooks": [{"type": "command", "command": "claude-ops hook protected-checkouts"}]},
                   {"matcher": "Bash|mcp__github__.*",
                    "hooks": [{"type": "command", "command": "claude-ops hook plain-voice"}]},
                   {"matcher": "Bash|mcp__github__merge_pull_request",
                    "hooks": [{"type": "command", "command": "claude-ops hook merge-gate"}]},
                   {"matcher": "Bash",
                    "hooks": [{"type": "command", "command": "claude-ops hook held-worktrees"}]}],
}


def _ago(ts: float) -> str:
    s = max(0, time.time() - ts)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{int(s // n)}{unit} ago"
    return "just now"


def _print_sessions(rows: list[dict], as_json: bool) -> None:
    if as_json:
        print(json.dumps(rows, indent=1))
        return
    for e in rows:
        live = " (maybe running)" if e.get("maybe_live") else ""
        print(f"{_ago(e.get('active_at') or e['mtime']):>9}  {sessions.label(e)[:60]:60}  {e['id']}{live}")
        print(f"{'':11}{sessions.resume_command(e)}")


def cmd_sessions(a) -> int:
    idx = sessions.annotate(sessions.build_index(force=a.refresh))
    if a.query:
        idx = sessions.find(idx, a.query)
    _print_sessions(idx[: a.limit], a.json)
    return 0


def cmd_search(a) -> int:
    hits = sessions.deep_search(a.text)
    idx = {e["id"]: e for e in sessions.annotate(sessions.build_index())}
    rows = sorted((idx[s] for s in hits if s in idx), key=lambda e: e.get("active_at") or e["mtime"], reverse=True)
    _print_sessions(rows[: a.limit], a.json)
    return 0 if rows else 1


def cmd_inject(a) -> int:
    if a.list:
        live = inject.iterm_sessions() if a.transport == "iterm" else inject.tmux_sessions()
        for k, v in sorted(live.items()):
            mark = "claude" if inject.looks_like_claude(a.transport, v) else "-"
            print(f"{mark:7} {k}\t{v.get('name', '')}")
        return 0
    if not a.target:
        print("--target is required", file=sys.stderr)
        return 2
    text = Path(a.file).read_text() if a.file else (a.text if a.text is not None else sys.stdin.read())
    r = inject.inject(a.target, text, transport=a.transport, enter=not a.no_enter, force=a.force, keep_text=a.keep_text)
    print(json.dumps(r))
    return {"sent": 0, "no-session": inject.NO_SESSION, "not-claude": inject.NOT_CLAUDE,
            "not-ready": inject.NOT_READY}.get(r["result"], 1)


def cmd_chat(a) -> int:
    if a.action == "list":
        for row in chat.listing():
            print(f"{row['tmux']:30} {row.get('command', ''):8} {'attached' if row.get('attached') else ''}")
        return 0
    if not a.name:
        print("a chat name is required", file=sys.stderr)
        return 2
    if a.action == "end":
        r = chat.end(a.name)
        print(json.dumps(r))
        return 0 if r["result"] == "ended" else 1
    if a.action == "show":
        ok, detail = chat.show_in_iterm(f"claude-{a.name}")
        print(detail)
        return 0 if ok else 1
    r = chat.new(a.name, Path(a.cwd), claude_args=a.args, trust=a.trust, show=not a.no_show, wait=a.wait)
    print(json.dumps(r, indent=1))
    if r.get("waiting_on"):
        print(f"\n{r['waiting_on']} {r['fix']}", file=sys.stderr)
    return 0 if r["result"] in ("started", "exists") else 1


def cmd_slots(a) -> int:
    root = Path(a.root) if a.root else slots.ROOT
    if a.release_stale:
        print("\n".join(slots.release_stale(root)) or "no stale locks")
        return 0
    if a.first_free:
        free = slots.first_free(root)
        print(free or "")
        return 0 if free else 1
    for s in slots.status(root):
        print(f"{s['slot']:16} {s['state']}" + (f" (pid {s['pid']})" if s["pid"] else ""))
    return 0


def cmd_restore(a) -> int:
    r = restore.restore(max_missing=a.max, dry_run=a.dry_run)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} {r['result']} missing={r['missing']} restored={len(r.get('restored', []))}")
    for p in r.get("restored", [])[:20]:
        print(f"  {p}")
    return 1 if r["result"] == "refused" else 0


def cmd_hook(a) -> int:
    if a.name == "no-session-links":
        from .hooks import no_session_links
        return no_session_links.main()
    if a.name == "public-voice":
        from .hooks import public_voice
        return public_voice.main([])
    if a.name == "protected-checkouts":
        from .hooks import protected_checkouts
        return protected_checkouts.main([])
    if a.name == "plain-voice":
        from .hooks import plain_voice
        return plain_voice.main([])
    if a.name == "merge-gate":
        from .hooks import merge_gate
        return merge_gate.main([])
    if a.name == "held-worktrees":
        from .hooks import held_worktrees
        return held_worktrees.main([])
    if a.name == "chain":
        from .hooks import chain
        return chain.main([])
    if a.name == "ask-what-you-need":
        from .hooks import ask_what_you_need
        return ask_what_you_need.main()
    if a.name == "open-question":
        from .hooks import open_question
        return open_question.main()
    from .hooks import context_monitor
    return context_monitor.main(["mark"] if a.name == "context-mark" else ["check"])


def _package_dir(name: str) -> Path:
    return Path(str(resources.files("claude_ops") / name))


def cmd_install(a) -> int:
    if a.what == "skills":
        dest = Path(a.dest).expanduser()
        for s in SKILLS:
            target = dest / s
            if target.exists() and not a.force:
                print(f"skip {target} (exists, use --force to replace)")
                continue
            shutil.rmtree(target, ignore_errors=True)
            shutil.copytree(_package_dir("skills") / s, target)
            print(f"installed {target}")
        return 0
    settings = Path(a.settings).expanduser()
    snippet = {"hooks": {k: v for k, v in HOOKS.items() if k in a.only or not a.only}}
    if not a.write:
        print(f"Add this to {settings} (or run again with --write):\n")
        print(json.dumps(snippet, indent=2))
        return 0
    data = json.loads(settings.read_text()) if settings.exists() else {}
    if settings.exists():
        shutil.copy2(settings, settings.with_name(settings.name + f".bak-{int(time.time())}"))
    hooks = data.setdefault("hooks", {})
    for event, entries in snippet["hooks"].items():
        have = json.dumps(hooks.get(event, []))
        for entry in entries:
            if entry["hooks"][0]["command"] not in have:
                hooks.setdefault(event, []).append(entry)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(data, indent=2) + "\n")
    print(f"hooks written to {settings} (backup kept next to it)")
    return 0


def cmd_rover(a) -> int:
    d = _package_dir("rover")
    if a.action == "path":
        print(d)
        return 0
    payload = Path(a.payload).resolve()
    if not payload.is_file():
        print(f"payload script not found: {payload}", file=sys.stderr)
        return 2
    cmd = ["caffeinate", "-dimsu", "bash", str(d / "harness.sh"), str(payload)]
    boot = open("/tmp/rover-boot.log", "ab")
    # a new session, so the mission outlives this shell when the network it runs over goes away
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=boot, stderr=boot, start_new_session=True)
    print(f"rover launched (pid {proc.pid}). The flight log is the newest run-*.log in the harness OUTDIR.")
    return 0


def cmd_unwrap_github(a) -> int:
    from . import github_unwrap
    out = github_unwrap.run(a.repo, write=a.apply, limit=a.limit)
    verb = "joined" if a.apply else "to join"
    print(f"{out['fixed'] if a.apply else out['found']} {verb}; failed {len(out['failed'])}; "
          f"archived and skipped {len(out['skipped'])}")
    for f in out["failed"][:20]:
        print(f"  failed: {f}")
    return 1 if out["failed"] else 0


def cmd_merge(a) -> int:
    from . import merge
    if len(a.numbers) == 1 and not a.confirm_prod:
        code, said = merge.merge_when_green(a.numbers[0], a.repo, method=a.method,
                                            timeout_s=a.timeout * 60, poll_s=a.poll,
                                            allow_no_checks=a.allow_no_checks,
                                            dependency_bump=a.dependency_bump,
                                            dependency_bots=tuple(merge.dependency_bump.UPDATERS) + tuple(a.bot or ()))
        print(said, file=sys.stdout if code == 0 else sys.stderr)
        return code
    if a.dependency_bump or a.allow_no_checks:
        print("--dependency-bump and --allow-no-checks apply to a single pull request only",
              file=sys.stderr)
        return 2
    code, said = merge.cascade(a.numbers, a.repo, method=a.method, timeout_s=a.timeout * 60,
                               poll_s=a.poll, confirm_prod=a.confirm_prod,
                               prod_environment=a.prod_environment)
    print(said, file=sys.stdout if code == 0 else sys.stderr)
    return code


def cmd_mcp_containers(a) -> int:
    from . import mcp_containers
    argv = (["--stop"] if a.stop else []) + ["--min-age", str(a.min_age)] + (["--json"] if a.json else [])
    return mcp_containers.main(argv)


def cmd_stall(a) -> int:
    from . import watch
    r = watch.stall(Path(a.path), minutes=a.minutes, max_hours=a.max_hours,
                    done_file=Path(a.done_file) if a.done_file else None, poll_s=a.poll)
    if a.json:
        print(json.dumps(r))
    else:
        print(f"{datetime.now():%H:%M} {r['line']}")
    return watch.STALL_EXIT[r["result"]]


def cmd_layering(a) -> int:
    from . import layering
    return layering.main(a.root, a.package, a.marker or [], a.skip or [], a.json)


def cmd_tab_colors(a) -> int:
    from claude_ops import iterm   # noqa: PLC0415
    done = iterm.color_chat_tabs(prefix=a.prefix)
    if done is None:
        print("iTerm2's Python API did not answer (switch it on in iTerm2's settings, and install "
              "the iterm extra)", file=sys.stderr)
        return 2
    for name, color in done.items():
        print(f"{name}: {color}")
    if not done:
        print("every shared window's tabs already differ")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="claude-ops", description="Tools for running many Claude Code chats on one machine.")
    p.add_argument("--version", action="version", version=f"claude-ops {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sessions", help="list chats from every folder, newest first")
    s.add_argument("query", nargs="?", help="filter by title, first message, folder or branch")
    s.add_argument("-n", "--limit", type=int, default=20)
    s.add_argument("--refresh", action="store_true", help="rebuild the index from scratch")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_sessions)

    s = sub.add_parser("search", help="search the full text of every chat")
    s.add_argument("text")
    s.add_argument("-n", "--limit", type=int, default=20)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("inject", help="send a message into a running chat")
    s.add_argument("--target", help="iTerm2 session id, part of its tab name, or a tmux session name")
    s.add_argument("--transport", choices=("iterm", "tmux"), default="iterm")
    s.add_argument("--text")
    s.add_argument("--file")
    s.add_argument("--no-enter", action="store_true", help="paste without submitting")
    s.add_argument("--force", action="store_true", help="send even if the target does not look like Claude Code")
    s.add_argument("--keep-text", action="store_true", help="store the text in the log (off by default, texts can carry secrets)")
    s.add_argument("--list", action="store_true", help="list the sessions that can be reached")
    s.set_defaults(func=cmd_inject)

    for name, (_, said) in TOOLS.items():   # parsed by their own modules (main), listed here for --help
        sub.add_parser(name, help=said, add_help=False)

    s = sub.add_parser("merge", help="merge a pull request only when every check on its head passed")
    s.add_argument("numbers", nargs="+", metavar="number",
                   help="one pull request number, or several to merge in turn (cascade)")
    s.add_argument("--repo", help="owner/name (default: the repository of the current folder)")
    s.add_argument("--method", choices=("merge", "squash", "rebase", "auto"), default="merge",
                   help="auto: the first of merge, squash and rebase the repository allows")
    s.add_argument("--timeout", type=float, default=30, help="minutes to wait for running checks")
    s.add_argument("--poll", type=float, default=20, help="seconds between checks")
    s.add_argument("--allow-no-checks", action="store_true",
                   help="merge a pull request that has no checks at all (one pull request only)")
    s.add_argument("--dependency-bump", choices=("patch", "minor"), default=None,
                   help="merge only a bot's update of lockfiles and manifests up to this level "
                        "(exit 6 otherwise; one pull request only)")
    s.add_argument("--bot", action="append", metavar="LOGIN",
                   help="another updater whose pull requests --dependency-bump may merge, by the login gh "
                        "prints (Dependabot and Renovate are known); repeatable")
    s.add_argument("--confirm-prod", action="store_true",
                   help="after the last merge, wait for that commit's checks and a deployment of it to succeed")
    s.add_argument("--prod-environment",
                   help="with --confirm-prod, keep only deployments whose environment name contains this")
    s.set_defaults(func=cmd_merge)

    s = sub.add_parser("stall", help="wait on a session's transcript until it finishes, stops on an "
                                      "error, or stalls (exit 0, 1 or 2)")
    s.add_argument("path", help="a transcript (.jsonl), a folder of them, or any file or folder a job writes")
    s.add_argument("--minutes", type=float, default=30, help="stalled after this long with no change")
    s.add_argument("--max-hours", type=float, default=12, help="give up after this long (exit 2)")
    s.add_argument("--done-file", help="a file the job writes when it is done, ends the wait once not empty")
    s.add_argument("--poll", type=float, default=60, help="seconds between checks")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_stall)

    s = sub.add_parser("layering", help="list a project's modules that reach no private marker "
                                        "(candidates to move into a package)")
    s.add_argument("root", help="the folder that holds the package")
    s.add_argument("--package", required=True, help="the top package to scan, e.g. app")
    s.add_argument("--marker", action="append", help="an import that means the private side, e.g. psycopg")
    s.add_argument("--skip", action="append", help="a module prefix to leave out, e.g. app.tests")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_layering)

    s = sub.add_parser("chat", help="start, show, end or list chats that run in tmux")
    s.add_argument("action", choices=("new", "show", "end", "list"))
    s.add_argument("name", nargs="?")
    s.add_argument("--cwd", default=".")
    s.add_argument("--args", default="", help="extra arguments for claude")
    s.add_argument("--trust", action="store_true", help="answer the trust-this-folder prompt with yes")
    s.add_argument("--no-show", action="store_true", help="do not open an iTerm2 window")
    s.add_argument("--wait", type=int, default=30)
    s.set_defaults(func=cmd_chat)

    s = sub.add_parser("tab-colors", help="give each chat tab in a shared iTerm2 window its own color")
    s.add_argument("--prefix", default="claude-", help="tmux session names that are chats")
    s.set_defaults(func=cmd_tab_colors)

    s = sub.add_parser("slots", help="Playwright MCP profile locks")
    s.add_argument("--root")
    s.add_argument("--first-free", action="store_true")
    s.add_argument("--release-stale", action="store_true")
    s.set_defaults(func=cmd_slots)

    s = sub.add_parser("restore", help="copy back missing chats from an archive")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--max", type=int, default=200)
    s.set_defaults(func=cmd_restore)

    s = sub.add_parser("mcp-containers", help="list or stop the MCP server containers no live chat runs")
    s.add_argument("--stop", action="store_true", help="stop the orphans (docker stop)")
    s.add_argument("--min-age", type=float, default=120, metavar="MINUTES",
                   help="leave a container younger than this alone (default 120)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_mcp_containers)

    s = sub.add_parser("hook", help="run a Claude Code hook (reads the event on stdin)")
    s.add_argument("name", choices=("ask-what-you-need", "context-check", "context-mark", "no-session-links",
                                    "public-voice", "protected-checkouts", "plain-voice", "open-question",
                                    "merge-gate", "held-worktrees", "chain"))
    s.set_defaults(func=cmd_hook)

    s = sub.add_parser("unwrap-github", help="join the broken paragraphs in what you posted on GitHub")
    s.add_argument("--repo", action="append", help="owner/name (repeatable); every owned one when absent")
    s.add_argument("--apply", action="store_true", help="rewrite them; without it, only report")
    s.add_argument("--limit", type=int, default=None, help="stop after this many")
    s.set_defaults(func=cmd_unwrap_github)

    s = sub.add_parser("install", help="install the skills, or the hooks into settings.json")
    s.add_argument("what", choices=("skills", "hooks"))
    s.add_argument("--dest", default="~/.claude/skills")
    s.add_argument("--force", action="store_true")
    s.add_argument("--settings", default="~/.claude/settings.json")
    s.add_argument("--only", nargs="*", default=[], choices=list(HOOKS))
    s.add_argument("--write", action="store_true")
    s.set_defaults(func=cmd_install)

    s = sub.add_parser("rover", help="where the rover scripts are, or launch a mission detached")
    s.add_argument("action", choices=("path", "launch"))
    s.add_argument("payload", nargs="?", default="mission.sh")
    s.set_defaults(func=cmd_rover)
    return p


#: commands whose module parses its own arguments: name -> (module, one line for --help)
TOOLS = {"union": ("union", "settle a rebase conflict where both sides only added lines"),
         "secret-shapes": ("secret_shapes", "refuse commits that add a line of a known secret shape"),
         "mutate": ("mutate", "change code on purpose and check the tests catch it"),
         "plan-apply": ("plan_apply", "apply a written plan's Write and Edit steps exactly"),
         "heavy": ("heavy", "run heavy jobs one at a time across chats, under a memory cap"),
         "restack": ("restack", "rebuild a stack of one-commit branches on a new base, settling add-only conflicts"),
         "detach": ("detach", "start a command in its own session, so it outlives the chat"),
         "adb-reach": ("adb", "reach an Android device over adb again after a restart undid tcpip"),
         "messages": ("messages", "what the person typed into chats, and the conversation around it"),
         "tracebacks": ("tracebacks", "Python tracebacks in log files, new since an offset, grouped"),
         "changelog": ("changelog", "the pull requests merged between two commits, oldest first"),
         "mcp-park": ("mcp_park", "move MCP servers between the configuration and a parked file, or keep only some"),
         "instructions": ("instruction_history", "what changed in the instruction files a chat loaded, from kept copies")}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in TOOLS:
        import importlib   # noqa: PLC0415
        mod = importlib.import_module(f"claude_ops.{TOOLS[argv[0]][0]}")
        return mod.main(argv[1:])
    a = build_parser().parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())

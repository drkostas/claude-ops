"""Every documented way to run a hook through the interpreter keeps the session's folder off the
import path (`python3 -P -m`): without -P, a file in the working folder named like a module the hook
imports would run in its place."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNSAFE = re.compile(r"\bpython3?(?![^\n]*?-P)\s+-m\s+claude_ops\.hooks\.")


def test_no_documented_hook_command_runs_without_isolation():
    found = []
    for f in [ROOT / "README.md", *sorted((ROOT / "src" / "claude_ops").rglob("*.py")),
              *sorted((ROOT / "src" / "claude_ops").rglob("*.md"))]:
        for n, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
            if UNSAFE.search(line):
                found.append(f"{f.relative_to(ROOT)}:{n}")
    assert found == []


def test_the_pattern_catches_the_unsafe_form_only():
    assert UNSAFE.search('"command": "python3 -m claude_ops.hooks.open_question"')
    assert UNSAFE.search("python -m claude_ops.hooks.public_voice --terms")
    assert not UNSAFE.search('"command": "python3 -P -m claude_ops.hooks.open_question"')
    assert not UNSAFE.search("claude-ops hook public-voice")

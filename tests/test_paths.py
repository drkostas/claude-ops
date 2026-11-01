"""Every module reads one transcript folder, and CLAUDE_OPS_PROJECTS moves all of them."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

NAMES = "chat.PROJECTS, restore.PROJECTS, sessions.ROOT, update.PROJECTS, worktree.PROJECTS"


def read(env: dict[str, str]) -> set[str]:
    code = ("from claude_ops import chat, restore, sessions, update, worktree\n"
            f"print(*map(str, ({NAMES})), sep=chr(10))")
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    return set(out.stdout.split())


def test_the_variable_moves_every_module(tmp_path):
    env = {**os.environ, "CLAUDE_OPS_PROJECTS": str(tmp_path)}
    assert read(env) == {str(tmp_path)}


def test_unset_every_module_reads_claude_codes_folder(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_OPS_PROJECTS"}
    env["HOME"] = str(tmp_path)
    assert read(env) == {str(Path(tmp_path) / ".claude" / "projects")}

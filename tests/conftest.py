import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True, scope="session")
def _no_real_tmux():
    """No test reaches the tmux server of the person running the suite. Inside a tmux pane, tmux
    follows $TMUX before TMUX_TMPDIR, so a test's `tmux kill-server` once ended every session on
    the machine. Every test runs with $TMUX unset and its own TMUX_TMPDIR."""
    saved = {k: os.environ.get(k) for k in ("TMUX", "TMUX_PANE", "TMUX_TMPDIR")}
    os.environ.pop("TMUX", None)
    os.environ.pop("TMUX_PANE", None)
    # a short folder: a socket path longer than 104 bytes fails on macOS
    private = tempfile.mkdtemp(prefix="co-", dir="/tmp")
    os.environ["TMUX_TMPDIR"] = private
    yield
    shutil.rmtree(private, ignore_errors=True)
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def write_chat(root: Path, folder: str, sid: str, records: list[dict]) -> Path:
    d = root / folder
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in records))
    return p


@pytest.fixture
def chat_tree(tmp_path):
    root = tmp_path / "projects"
    write_chat(root, "-work-alpha", "aaa", [
        {"type": "user", "cwd": "/work/alpha", "gitBranch": "main", "timestamp": "2026-01-01T10:00:00Z",
         "message": {"content": "fix the login page"}},
        {"type": "assistant", "timestamp": "2026-01-01T10:05:00Z", "message": {"content": [{"type": "text", "text": "done"}]}},
        {"customTitle": "alpha-login"},
    ])
    write_chat(root, "-work-beta", "bbb", [
        {"type": "user", "cwd": "/work/beta", "timestamp": "2026-02-01T09:00:00Z",
         "message": {"content": [{"type": "text", "text": "<command-name>/clear</command-name>"}]}},
        {"type": "user", "timestamp": "2026-02-01T09:01:00Z", "message": {"content": "write the backup script"}},
        {"type": "assistant", "timestamp": "2026-02-01T09:30:00Z", "message": {"content": "the word zebra appears here"}},
    ])
    return root

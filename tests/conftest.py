import json
from pathlib import Path

import pytest


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

"""Where Claude Code keeps its transcripts: one definition for every module here.

Set CLAUDE_OPS_PROJECTS before importing claude_ops to point everything at another folder, such as a
test's fixture folder.
"""
from __future__ import annotations

import os
from pathlib import Path

#: the folder holding one subfolder of transcripts per project
PROJECTS = Path(os.environ.get("CLAUDE_OPS_PROJECTS") or Path.home() / ".claude" / "projects")

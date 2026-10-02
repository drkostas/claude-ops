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
    return {"sent": 0, "no-session": inject.NO_SESSION, "not-claude": inject.NOT_CLAUDE}.get(r["result"], 1)


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
    if a.name == "ask-what-you-need":
        from .hooks import ask_what_you_need
        return ask_what_you_need.main()
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

    s = sub.add_parser("chat", help="start, show, end or list chats that run in tmux")
    s.add_argument("action", choices=("new", "show", "end", "list"))
    s.add_argument("name", nargs="?")
    s.add_argument("--cwd", default=".")
    s.add_argument("--args", default="", help="extra arguments for claude")
    s.add_argument("--trust", action="store_true", help="answer the trust-this-folder prompt with yes")
    s.add_argument("--no-show", action="store_true", help="do not open an iTerm2 window")
    s.add_argument("--wait", type=int, default=30)
    s.set_defaults(func=cmd_chat)

    s = sub.add_parser("slots", help="Playwright MCP profile locks")
    s.add_argument("--root")
    s.add_argument("--first-free", action="store_true")
    s.add_argument("--release-stale", action="store_true")
    s.set_defaults(func=cmd_slots)

    s = sub.add_parser("restore", help="copy back missing chats from an archive")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--max", type=int, default=200)
    s.set_defaults(func=cmd_restore)

    s = sub.add_parser("hook", help="run a Claude Code hook (reads the event on stdin)")
    s.add_argument("name", choices=("ask-what-you-need", "context-check", "context-mark"))
    s.set_defaults(func=cmd_hook)

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


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())

"""Let a session draft mail freely and send it only where a rule or a one-time approval allows it
(a PreToolUse hook).

A session that can send mail can send it anywhere. A message in the inbox can ask it to, a slip can
name the wrong person, and a sent message cannot be taken back. A draft is different: a person reads
it before it goes. So this hook lets every draft through and writes one log line for it, and lets a
send through only when

- a rule allows it: the account, the thread and every recipient (To, Cc and Bcc) match one rule, or
- a one-time approval allows it: an approval record for exactly this message (the account, the
  recipients, the thread and a hash of the body) that is not used yet and has not expired. The hook
  marks the record used, so one approval lets one message through.

Anything else is refused, with the reason and what would let it through. The hook does not decide
who may approve. An approval record is a JSON file that whatever the person approves with writes
into the approvals folder, for example:

    {"account": "work", "recipients": ["someone@example.org"], "thread": "",
     "body_sha256": "<hex digest of the body as UTF-8>", "expires": "2026-01-01T12:30:00+00:00"}

`mode` "observe" checks and logs every call the same way but refuses nothing, which shows what a
rule set would refuse before it is enforced. Each log line is one JSON object with the time, the
tool, the decision (draft, rule, approval, refused, would-refuse), the account, the recipients, the
thread, the subject and the body's hash.

The configuration is a JSON file:

    {
      "mode": "enforce",
      "send_tools": ["mcp__mail__send_email"],
      "draft_tools": ["mcp__mail__create_draft", "mcp__mail__update_draft"],
      "rules": [{"name": "weekly report", "account": "work", "thread": "18c2f00000000000",
                 "recipients": ["reports@example.org"]}],
      "approvals": "~/.config/claude-ops/mail-approvals",
      "log": "~/.local/state/claude-ops/mail-guard.jsonl",
      "message": "Draft it for the person to send, or ask for an approval of this message."
    }

A rule without `account` or `thread` matches any. The tool's input is read as `account`, `to`, `cc`,
`bcc`, `thread_id`, `subject` and `body`. The file is `--config`, else `CLAUDE_OPS_MAIL_GUARD_CONFIG`,
else `~/.config/claude-ops/mail-guard.json`. With no readable configuration nothing is checked: a
broken hook must never stop work.

    python3 -P -m claude_ops.hooks.mail_guard [--config FILE]
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from email.utils import getaddresses
from pathlib import Path

DEFAULT_CONFIG = "~/.config/claude-ops/mail-guard.json"
DEFAULT_LOG = "~/.local/state/claude-ops/mail-guard.jsonl"


def load(argv: list[str]) -> dict | None:
    """The configuration from `--config`, the environment or the default file; None when unreadable."""
    path = None
    for i, a in enumerate(argv):
        if a == "--config" and i + 1 < len(argv):
            path = argv[i + 1]
    path = path or os.environ.get("CLAUDE_OPS_MAIL_GUARD_CONFIG") or DEFAULT_CONFIG
    try:
        with open(os.path.expanduser(path)) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return None
    return cfg if isinstance(cfg, dict) else None


def recipients(tool_input: dict) -> list[str]:
    """Every address in To, Cc and Bcc, in lower case, once each, in order."""
    fields = [str(tool_input.get(k) or "").replace(";", ",") for k in ("to", "cc", "bcc")]
    fields = [f for f in fields if f.strip()]
    try:
        # strict parsing (the default since Python 3.13) turns a whole list into one empty pair when
        # any part of it is unusual, which would read as "no recipients"
        pairs = getaddresses(fields, strict=False)
    except TypeError:  # an older Python without the strict argument
        pairs = getaddresses(fields)
    out: list[str] = []
    for _, addr in pairs:
        addr = addr.strip().lower()
        if addr and addr not in out:
            out.append(addr)
    return out


def body_hash(tool_input: dict) -> str:
    return hashlib.sha256(str(tool_input.get("body") or "").encode("utf-8")).hexdigest()


def rule_for(cfg: dict, account: str, thread: str, to: list[str]) -> str | None:
    """The name of the first rule that allows this send, or None."""
    if not to:
        return None
    for i, r in enumerate(cfg.get("rules") or []):
        if not isinstance(r, dict):
            continue
        if r.get("account") and r["account"] != account:
            continue
        if r.get("thread") and r["thread"] != thread:
            continue
        allowed = {str(a).strip().lower() for a in r.get("recipients") or []}
        if allowed and set(to) <= allowed:
            return str(r.get("name") or f"rule {i + 1}")
    return None


def use_approval(cfg: dict, account: str, thread: str, to: list[str], digest: str, now: datetime,
                 *, mark: bool = True) -> str | None:
    """The file name of an unused, unexpired approval for exactly this message, marked used; or None."""
    folder = cfg.get("approvals")
    if not folder:
        return None
    root = Path(os.path.expanduser(folder))
    if not root.is_dir():
        return None
    for f in sorted(root.glob("*.json")):
        try:
            rec = json.loads(f.read_text())
            if not isinstance(rec, dict) or rec.get("used"):
                continue
            if datetime.fromisoformat(str(rec["expires"])) <= now:
                continue
            if (rec.get("account", "") != account or (rec.get("thread") or "") != thread
                    or rec.get("body_sha256") != digest
                    or sorted(str(a).strip().lower() for a in rec.get("recipients") or []) != sorted(to)):
                continue
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if mark:
            rec["used"] = now.isoformat()
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(rec, indent=1))
            os.replace(tmp, f)
        return f.name
    return None


def decide(cfg: dict, payload: dict, now: datetime | None = None) -> tuple[str, str]:
    """(decision, detail) for one tool call: "pass" for a tool the guard does not cover, "draft",
    "rule", "approval", "refused", or "would-refuse" in observe mode."""
    now = now or datetime.now(timezone.utc)
    tool = str(payload.get("tool_name") or "")
    ti = payload.get("tool_input") or {}
    if tool in (cfg.get("draft_tools") or []):
        return "draft", ""
    if tool not in (cfg.get("send_tools") or []):
        return "pass", ""
    account, thread = str(ti.get("account") or ""), str(ti.get("thread_id") or "")
    to = recipients(ti)
    name = rule_for(cfg, account, thread, to)
    if name:
        return "rule", name
    observe = cfg.get("mode") == "observe"
    used = use_approval(cfg, account, thread, to, body_hash(ti), now, mark=not observe)
    if used:
        return "approval", used
    why = (f"no rule allows a send from {account or 'this account'} to {', '.join(to) or 'nobody'}"
           + (f" in thread {thread}" if thread else "") + ", and no unused approval matches this exact message")
    return ("would-refuse" if observe else "refused"), why


def write_log(cfg: dict, payload: dict, decision: str, detail: str, now: datetime) -> None:
    ti = payload.get("tool_input") or {}
    line = {"time": now.isoformat(timespec="seconds"), "tool": payload.get("tool_name"), "decision": decision,
            "detail": detail, "account": ti.get("account"), "recipients": recipients(ti),
            "thread": ti.get("thread_id"), "subject": ti.get("subject"), "body_sha256": body_hash(ti)}
    path = Path(os.path.expanduser(cfg.get("log") or DEFAULT_LOG))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    try:
        cfg = load(list(sys.argv[1:] if argv is None else argv))
        if cfg is None:
            return 0
        payload = json.load(sys.stdin)
        now = datetime.now(timezone.utc)
        decision, detail = decide(cfg, payload, now)
    except Exception:  # noqa: BLE001 - a broken hook must never stop work
        return 0
    if decision != "pass":
        try:
            write_log(cfg, payload, decision, detail, now)
        except OSError:
            pass  # a log that cannot be written never turns a refusal into a pass
    if decision == "refused":
        extra = cfg.get("message") or "Draft it for the person to send, or ask for an approval of this exact message."
        print(f"Blocked: {detail}. {extra}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

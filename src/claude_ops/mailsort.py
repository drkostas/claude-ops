"""Sort the mail a mailbox receives about software projects, and read the notices platforms send.

A maintainer's inbox mixes several kinds of mail about the same projects: a secret scanner's
incident, a hosting platform's warning that a quota is nearly spent or a deploy failed, and a person
writing about a repository. Each needs a different reader, and a watcher that only knows "unread"
treats them all as one. This module answers two questions about one message, with only the standard
library:

- `Sorter.sort` says what kind of mail it is and which of the user's projects it names, from its
  headers and text. The kinds and the project names are configuration: the user lists the senders
  of each kind and the names (with aliases) of each project, so nothing here knows any one person's
  repositories. Mail that names a project from no configured sender is "person" when typed and
  "mention" when sent by a program.
- The notice readers (`neon_usage`, `vercel_deploy_failed`, `gitguardian_incidents`) turn a
  platform's own notice mail into the fact it states, such as "project X has used 80% of its monthly
  compute", so a watcher can record the fact instead of the mail.

    from claude_ops.mailsort import Sorter
    sorter = Sorter(kinds={"scanner": ["gitguardian.com"], "platform": ["vercel.com", "neon.tech"]},
                    names={"widget": ["widget-cli"]}, has_reader=["github.com"])
    got = sorter.sort({"From": "Ana <ana@example.org>", "Subject": "A question about widget"}, "...")
    got.kind, got.names        # ("person", ["widget"])

Nothing here reads a mailbox (see `claude_ops.gmail`) or writes anything.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from email.utils import parseaddr

#: the local parts automated senders use; a mail from one is never "a person writing"
AUTOMATED_LOCAL = re.compile(
    r"^(no-?reply|do-?not-?reply|notifications?|notify|alerts?|mailer-daemon|postmaster|bounces?|"
    r"news(letter)?|updates?|info|support|hello|team|marketing|billing|security)([+._-].*)?$",
    re.IGNORECASE)
#: Gmail's own category labels for bulk mail
BULK_LABELS = ("CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS")


def address(from_header: str) -> str:
    """Pure. The address a From header names, lower-cased; "" when it names none."""
    return parseaddr(from_header or "")[1].strip().lower()


def domain(from_header: str) -> str:
    """Pure. The domain of the address a From header names, lower-cased."""
    sender = address(from_header)
    return sender.rpartition("@")[2] if "@" in sender else ""


def _matches(sender: str, patterns) -> bool:
    """Pure. Whether an address matches one of the patterns: a whole address, a domain (which also
    matches its subdomains, so "neon.tech" matches "alerts@email.neon.tech"), or "@domain"."""
    host = sender.rpartition("@")[2]
    for p in patterns:
        p = p.strip().lower().lstrip("@")
        if not p:
            continue
        if "@" in p:
            if sender == p:
                return True
        elif host == p or host.endswith("." + p):
            return True
    return False


def is_automated(headers: dict) -> bool:
    """Pure. Whether a message was sent by a program rather than typed by a person: an automated
    sender name (noreply, notifications, ...), a List-Unsubscribe or List-Id header, Precedence bulk
    or list, or an Auto-Submitted header other than "no"."""
    h = {k.lower(): v for k, v in (headers or {}).items()}
    local = address(h.get("from", "")).partition("@")[0]
    if AUTOMATED_LOCAL.match(local):
        return True
    if h.get("list-unsubscribe") or h.get("list-id"):
        return True
    if (h.get("precedence") or "").strip().lower() in ("bulk", "list", "junk"):
        return True
    auto = (h.get("auto-submitted") or "").strip().lower()
    return bool(auto) and auto != "no"


def is_bulk(labels) -> bool:
    """Pure. Whether Gmail filed the message as promotions, social or forums mail."""
    return any(lb in BULK_LABELS for lb in labels or ())


@dataclass
class Sorted:
    """What `Sorter.sort` found. `kind` is one of the configured kinds, "person", "mention", or None
    (nothing for a project reader: no project named, mail that already has a reader, or the user's
    own).
    `names` are the projects the message names, in the order configured. `why` says which rule
    decided."""
    kind: str | None
    names: list[str] = field(default_factory=list)
    sender: str = ""
    why: str = ""


class Sorter:
    """Sorts one message at a time.

    `kinds` maps a kind ("scanner", "platform", ...) to sender patterns (addresses or domains); the
    first kind whose pattern matches wins. `names` maps each project to the extra words that also
    name it (its package name, an old spelling); the project's own name always counts. `has_reader`
    lists senders some other watcher already reads, so their mail is left alone. `own` lists the
    user's own addresses, whose mail is never sorted. A message from a sender of no kind that names
    a project is a person writing about it ("person"), or "mention" when it was sent by a program
    (a newsletter, or a person's message sent through a mailing service, which look the same).

    A kind's mail is kept even when it names no project (a quota warning names a platform project,
    not a repository); a person's mail only when it names one."""

    def __init__(self, kinds: dict | None = None, names: dict | None = None,
                 has_reader=(), own=()):
        self.kinds = {k: list(v) for k, v in (kinds or {}).items()}
        self.has_reader = list(has_reader)
        self.own = {a.strip().lower() for a in own}
        self._names = []
        for name, aliases in (names or {}).items():
            words = sorted({name, *(aliases or ())}, key=len, reverse=True)
            # a name is a whole word: "soma" names soma, never "somatic"; hyphens and dots are
            # part of a name, so "garmin" does not name "garmin-auth"
            rx = re.compile(r"(?<![\w.-])(" + "|".join(re.escape(w) for w in words) + r")(?![\w-]|\.\w)",
                            re.IGNORECASE)
            self._names.append((name, rx))

    def names_in(self, text: str) -> list[str]:
        """Pure. The configured projects a text names, in the order they were configured."""
        return [name for name, rx in self._names if rx.search(text or "")]

    def sort(self, headers: dict, text: str = "") -> Sorted:
        """Pure. Sort one message from its headers (by name, any case) and its text."""
        h = {k.lower(): v for k, v in (headers or {}).items()}
        sender = address(h.get("from", ""))
        if sender in self.own:
            return Sorted(None, sender=sender, why="the user's own mail")
        if _matches(sender, self.has_reader):
            return Sorted(None, sender=sender, why="another watcher reads this sender")
        names = self.names_in(f"{h.get('subject', '')}\n{text or ''}")
        for kind, patterns in self.kinds.items():
            if _matches(sender, patterns):
                return Sorted(kind, names, sender, f"the sender is a {kind}")
        if not names:
            return Sorted(None, sender=sender, why="it names no project")
        if is_automated(headers):
            # a person's pitch sent through a mailing service carries the same headers as a
            # newsletter, so naming a project is kept apart as a mention rather than dropped
            return Sorted("mention", names, sender, "automated mail that names a project")
        return Sorted("person", names, sender, "a person who names a project")


# --- notice readers: each takes the subject and the text, and answers the fact or None ----------

_NUM = r"(\d+(?:\.\d+)?)"


def neon_usage(subject: str, text: str) -> dict | None:
    """Pure. Neon's monthly allowance notice: {"project", "metric", "pct", "used", "allowance",
    "unit"}, or None when the mail is not one. `metric` is "compute" (unit CU-hours) or "transfer"
    (public network transfer, unit GB); `used` is None when the notice gives only the share. Neon
    sends one when a project nears its allowance (80% for compute) and one when it reaches it, and
    the project's queries fail from then until the allowance resets next month."""
    t = " ".join(f"{subject or ''} {text or ''}".split())
    head = r"Your project (\S+?)\s*(?:\(.*?\))?\s+has used " + _NUM + r"%\s*(?:\(" + _NUM + r" ?(?:CU-hours?|GB)\))?\s+of its "
    m = re.search(head + _NUM + r" CU-hours? monthly compute allowance", t)
    if m:
        metric, unit = "compute", "CU-hours"
    else:
        m = re.search(head + r"(?:monthly )?" + _NUM + r" ?GB (?:monthly )?(?:public )?network transfer allowance", t)
        if not m:
            return None
        metric, unit = "transfer", "GB"
    return {"project": m.group(1), "metric": metric, "pct": float(m.group(2)),
            "used": float(m.group(3)) if m.group(3) else None, "allowance": float(m.group(4)), "unit": unit}


def vercel_deploy_failed(subject: str, text: str) -> dict | None:
    """Pure. Vercel's failed deployment notice: {"project", "target", "branch", "commit"} (branch and
    commit None when not given), or None when the mail is not one."""
    s = re.search(r"(Production|Preview) deployment failed for (\S+)", subject or "")
    if not s:
        return None
    t = " ".join((text or "").split())
    b = re.search(r"on branch (\S+?) at commit ([0-9a-f]{7,40})", t)
    return {"project": s.group(2), "target": s.group(1).lower(),
            "branch": b.group(1) if b else None, "commit": b.group(2) if b else None}


def gitguardian_incidents(subject: str, text: str) -> list[dict]:
    """Pure. The incidents a GitGuardian alert lists: [{"kind", "repo", "commit"}], one per
    occurrence it names (a mail can list several), or [] when the mail is not one. `repo` is
    "owner/name" as the alert writes it."""
    if "incident" not in (subject or "").lower():
        return []
    t = " ".join((text or "").replace("​", "").split())
    # the alert lists each incident as "<kind> <date> <time> (UTC)" and then its occurrences, each
    # "<owner>/<repo> (commit <sha>)"; the kind is the words before the date, after whatever ended
    # the incident before it
    parts = re.split(r"\s*\d{4}-\d{2}-\d{2} [\d:]+ (?:[AP]M )?\(UTC\)\s*", t)
    out = []
    for before, after in zip(parts, parts[1:]):
        kind = re.split(r"detected!|See on GitHub|other occurrences?", before)[-1].strip()
        for r in re.finditer(r"([\w.-]+)/\s?([\w.-]+) \(commit ([0-9a-f]{7,40})\)", after):
            out.append({"kind": kind, "repo": f"{r.group(1)}/{r.group(2)}", "commit": r.group(3)})
    return out

"""plan_apply — apply the Write and Edit steps of a written plan, exactly as the plan states them.

An implementation plan written for an agent often carries every new file as a step "(Write `<path>`)"
followed by one fenced block, and every change as "(Edit on `<path>`)" followed by an old-text block
and a new-text block. Applying such a plan by hand is many copy-pastes, and a retyped edit can drift
from the plan that was reviewed. This reads the plan literally:

- a Write writes the first block after its header, exactly;
- an Edit replaces the first block with the second, and refuses unless the old text is in the file
  exactly once;
- a step that is neither, or one that cannot be applied, stops the run, so nothing after it is half
  done.

Run steps (tests, scripts) stay with whoever executes the plan, who compares their output with the
plan's expected lines. A step header is a markdown checkbox item starting "**Step", for example
"- [ ] **Step 3: Write the tokens** (Write `src/tokens.py`)".

A step is named by its header's line number or by words from its header. Words are the safer of the
two: a plan that is still being edited moves under line numbers.

    claude-ops plan-apply PLAN.md 92 442 'Step 4: Route it'   # apply in this order
    claude-ops plan-apply PLAN.md --block 'Step 1: Write the fixtures'   # print a block
    claude-ops plan-apply PLAN.md --steps          # list every step with its line
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

WRITE = re.compile(r"\(Write `([^`]+)`\)")
EDIT = re.compile(r"\(Edit on `([^`]+)`")
STEP = re.compile(r"^\s*- \[[ x]\] \*\*Step")


class Refused(Exception):
    """A step that cannot be applied as written; the message says why."""


def steps(lines: list[str]) -> list[tuple[int, str]]:
    """(1-based line, header) of every step in the plan."""
    return [(i + 1, l.strip()) for i, l in enumerate(lines) if STEP.match(l)]


def find(lines: list[str], ref: str) -> int:
    """The 1-based line of a step named by its line number or by words from its header. Words that
    match no header, or more than one, are refused rather than guessed."""
    if ref.isdigit():
        n = int(ref)
        if not 1 <= n <= len(lines):
            raise Refused(f"line {n} is outside the plan ({len(lines)} lines)")
        return n
    hits = [n for n, h in steps(lines) if ref in h]
    if len(hits) != 1:
        raise Refused(f"{len(hits)} step headers hold {ref!r}, not one: name it by its line")
    return hits[0]


def block(lines: list[str], line: int, nth: int = 1) -> str:
    """The nth fenced block after a 1-based line, its text exactly, the fences left out."""
    i, seen = line, 0
    while i < len(lines):
        s = lines[i].lstrip()
        if s.startswith("```"):
            fence = s[: len(s) - len(s.lstrip("`"))]
            j = i + 1
            while j < len(lines) and not lines[j].lstrip().startswith(fence):
                j += 1
            if j >= len(lines):
                raise Refused(f"the block opened at line {i + 1} is never closed")
            seen += 1
            if seen == nth:
                return "".join(l + "\n" for l in lines[i + 1: j])
            i = j + 1
            continue
        i += 1
    raise Refused(f"no block {nth} after line {line}")


def apply(lines: list[str], line: int) -> str:
    """Apply the Write or Edit step whose header is at `line`. Returns what it did."""
    header = lines[line - 1]
    if m := WRITE.search(header):
        target = Path(m.group(1))
        body = block(lines, line)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        return f"wrote {target} ({len(body.splitlines())} lines, step at line {line})"
    if m := EDIT.search(header):
        target = Path(m.group(1))
        old, new = block(lines, line, 1), block(lines, line, 2)
        old, new = old[:-1] if old.endswith("\n") else old, new[:-1] if new.endswith("\n") else new
        if not target.is_file():
            raise Refused(f"{target} does not exist (step at line {line})")
        text = target.read_text(encoding="utf-8")
        if text.count(old) != 1:
            raise Refused(f"{target} holds the old text {text.count(old)} times, not once "
                          f"(step at line {line}): {old[:80]!r}")
        target.write_text(text.replace(old, new), encoding="utf-8")
        return f"edited {target} (step at line {line})"
    raise Refused(f"line {line} is neither a Write nor an Edit step: {header.strip()[:100]!r}")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__.split("\n\n")[-1])
        return 2
    plan = Path(argv[0])
    lines = plan.read_text(encoding="utf-8").splitlines()
    try:
        if argv[1] == "--steps":
            for n, h in steps(lines):
                print(f"{n}: {h[:150]}")
            return 0
        if argv[1] == "--block":
            nth = int(argv[3]) if len(argv) > 3 else 1
            sys.stdout.write(block(lines, find(lines, argv[2]), nth))
            return 0
        for ref in argv[1:]:
            print(apply(lines, find(lines, ref)))
    except Refused as e:
        print(f"STOPPED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

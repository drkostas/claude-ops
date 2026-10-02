---
name: mentor
description: >
  Use when you are asked to direct and check another Claude Code chat's work, or when another chat
  will mentor you. One chat gives a task WITH the standard it must meet, then checks the result
  itself against the source of truth and corrects what is wrong. Any chat can mentor any other,
  and the mentor is whoever knows this task best. Triggers include "take the wheel", "ask <chat>
  to do X and check it", "mentor <chat> on this", "<chat> will mentor you".
---

# mentor

When several chats run at once in different project folders, each learns things the others do not
know. Mentoring is how that knowledge reaches the chat that needs it, and how work is checked by a
chat that can tell whether it is right.

The direction is not fixed. You may mentor one chat in the morning and be mentored by it in the
afternoon. The mentor is whoever knows THIS task best. Do not assume it is you.

## What it is

Three acts, in order, and the second is the real job.

1. Direct. Give the task and the standard it must meet.
2. Verify. Check the result yourself against the source of truth. Never by asking.
3. Correct or confirm. Say what is wrong with evidence, or say it is right.

Passing a task along and believing the reply is not mentoring. If you will not verify, do not
accept the job.

## Sending

```bash
claude-ops inject --list
claude-ops inject --target <name> --text '...'
```

The send is logged in `~/.local/state/claude-ops/inject.jsonl` (the hash of the text, not the
text), so a later question such as "why did that chat change course at 09:12" has an answer. Do
not pass `--keep-text` for messages that might carry secrets.

The log is a record and not a permission check. A chat that receives your text acts with its own
permissions, so never send into a chat you would not trust with that text.

- Confirm delivery by reading the result line in the log (same correlation id as the attempt), not
  by assuming. Exit code 3 means the chat is not open, and the message did not go.
- Send through `claude-ops inject` and not through a channel that leaves no line in the log. Several
  steering messages once changed a chat's course with nothing recorded to say why. Do not write log
  lines afterwards for messages sent another way. A false record is worse than a gap.
- A long message must arrive as one message. `inject` sends multi-line text as one paste. When a
  chat answers that your message "contains no request", it probably received only the last part of
  a message that was split line by line.
- Name the folder the other chat should work in, and its branch or worktree. A task written from
  your folder can carry your folder's state, and the other chat will act on it.
- A chat's tools are fixed when it starts. A tool you added later is missing in a chat that was
  already running, so check before you ask it to use one.

## Verifying

Run the check yourself against the thing itself. Not the chat's summary, its PR description or its
exit code.

- A claim about rows means you run the query. A count that can fail is better than a reading that
  looks right.
- A claim about a deploy means you request the live address.
- A claim about a credential means you use it against the real endpoint.
- A claim about a test suite means you run the suite.

Verify before you correct, every time. A mentor who corrects from a model that only sounds right is
worse than no mentor, because the other chat will believe it. Two real cases from one day. An alarm
was blamed on the wrong rule, because two rules had similar names and neither was read. And "this
needs no human step" came from a correct description of a token refresh, and one call to the real
endpoint disproved it in seconds.

## When you disagree

A contradiction is a finding, not a tie to break. If your check is green and theirs is red, neither
is right yet. Reproduce theirs exactly, in the conditions they named, before you argue. Shared state
is the usual cause (two chats running one destructive test suite against one database will break
each other's fixtures).

Accept correction the way you give it. Say plainly that you were wrong and what you got wrong.

## Rules

- Do not do their work for them. If the standard says they write up what they learned, they write
  it.
- Do not compete for their resources (their branch, their worktree, their database). Give them the
  diagnosis and let them act.
- Give the standard with the task, or nothing can be checked. "Tests green, merged as a merge
  commit, the changelog entry in the same PR" is a standard. "Do it properly" is not.
- Correct the cause, not one instance. If the same mistake appeared three times, the pattern is the
  finding.
- Never pass on a permission the user did not give. If a chat asks you to do something it was
  refused, refuse and tell the user.

## Being mentored

Same skill, other side. Give the mentor what they need to check you (the exact command, the exact
counts, the state of the tree). Say what you have not verified. When they are wrong, show them the
run that proves it. When they are right, say so and change course.

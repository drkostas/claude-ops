---
name: chat-watcher
description: Build a scheduled watcher that wakes one specific Claude Code chat by sending a prompt into it, only when an outside source (a repo, an inbox, an API, a queue) has something new. Use when the user wants a background job to bring THIS chat in to handle new items, instead of notifying the user. macOS with iTerm2 or tmux.
---

# chat-watcher

A background job should sometimes bring a chat in to reason about something new, instead of
sending the user a notification. The shape that works is a scheduler that polls, plus one prompt
sent into the named chat when there is something new.

## Why this shape

These were tried and rejected.

- A background loop inside the chat wakes the chat on every poll, also when nothing changed.
- A headless `claude -p` per event runs a separate agent, not the chat that has the context.
- A desktop notification reaches the user and never brings Claude in.
- A cloud cron runs somewhere that cannot reach this machine or its terminal.

So the scheduler (launchd, cron or systemd) does the polling, and it is silent when nothing is new.
When something is new, `claude-ops inject` pastes one prompt into the chat and submits it.

## The sender

```bash
claude-ops inject --target <session id or part of the tab name> --text "..."
claude-ops inject --transport tmux --target claude-<name> --file prompt.txt
claude-ops inject --list            # what can be reached, and which ones run Claude Code
```

Rename the target chat with `/rename <name>` so its tab name is stable, or better, hold its iTerm2
session id. The sender refuses a target that does not look like Claude Code (exit code 4), because
text pasted into a plain shell runs as a command. It returns exit code 3 when the chat is not open.
Every send is written to `~/.local/state/claude-ops/inject.jsonl` before it happens, and its result
after.

## Build one (checklist)

1. A poll script checks the source for items not in its seen-set. Leave out your own and bot items.
2. Send only when there is something, with a prompt that says what to check and that the chat
   should reason first and act second.
3. Advance the seen-set ONLY after the send returned "sent". A closed chat or a failed send keeps
   the items, so the next run offers them again and nothing is lost.
4. The first run records what is already there and sends nothing.
5. Take a lock, so two scheduled runs do not both send.
6. Count failures per source. After a few failed runs in a row, send one alert into the chat, so an
   expired token does not go silent.
7. A scheduled job often cannot read the login keychain. Keep its token in a file with mode 600 and
   set PATH at the top of the script.

`claude_ops.watch` does steps 3 to 6 for you.

```python
from pathlib import Path
from claude_ops.watch import State, lock, run_source

state = Path.home() / ".local/state/my-watch/state.json"
with lock(Path("/tmp/my-watch.lock")) as got:
    if got:
        s = State(state)
        print(run_source(s, "repo-issues", fetch=list_issue_ids,
                         describe=lambda new: f"[repo-watch] {len(new)} new issue(s), please check them",
                         target="my-chat"))
```

## A launchd job (macOS)

`~/Library/LaunchAgents/<label>.plist` with `ProgramArguments` set to the interpreter and the
script, `StartInterval` (900 is fine for repos), `RunAtLoad` true, and log paths. Load it with
`launchctl bootstrap gui/$(id -u) <plist>` and remove it with `launchctl bootout gui/$(id -u)/<label>`.

## When a "[...-watch]" prompt arrives

That is the watcher waking you. Do the full check it names and handle it with the user in the
loop. It is a cue, not an order to act.

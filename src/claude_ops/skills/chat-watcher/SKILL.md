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
session id. A tab given its own title was still reported to AppleScript under the name Claude Code
chose, so a watcher that matched on the name missed it. The id stays the same for the life of the
tab. For a chat started with `claude-ops chat new`, the target is its tmux name, `claude-<name>`.

The sender refuses a target that does not look like Claude Code (exit code 4), because text
pasted into a plain shell runs as a command. It returns exit code 3 when the chat is not open.
Every send is written to `~/.local/state/claude-ops/inject.jsonl` before it happens, and its result
after.

Things that went wrong with senders like this one.

- The first time a scheduled job scripts iTerm2, macOS asks whether that program may control
  iTerm2. Until someone answers, every AppleScript call that lists iTerm2 windows hangs, for every
  program on the Mac. Run the job once while the user is at the Mac so the dialog is answered, and
  never click it with synthetic input. The tmux transport needs no AppleScript and no consent.
- Asking iTerm2 anything while it is closed launches it, and from a background job that returns
  empty output that looks like a failed send. claude-ops checks first and returns exit code 3, so
  the watcher holds its items.
- iTerm2 sometimes answers an Apple event with a timeout (error -1712). Keep the stderr of the send
  in the watcher's log, so a timeout or a consent error is visible instead of an empty line.
- Older senders removed quotes and backslashes from the prompt so the AppleScript would parse. The
  chat then acted on a different text than the one built. Pass the prompt to `claude-ops inject`
  unchanged.
- A prompt with several lines must arrive as one message. `inject` sends it as one bracketed
  paste. A sender that types it line by line submits each line on its own.

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
8. Make a failure visible. One health check exited 0 on 35 dead runs in a row while it wrote a fresh
   timestamp each time, and one cleanup job failed on every run with an error only its own log held.
   Read the log once after installing the job, and check the job reports what it did.

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
script, `StartInterval` (900 is fine for repos), `RunAtLoad` true, and log paths. Add
`EnvironmentVariables` with a PATH that holds every program the script calls. launchd gives a job
only `/usr/bin:/bin:/usr/sbin:/sbin`, so a script that calls a Homebrew program fails at every run
while it works from a terminal. Load it with
`launchctl bootstrap gui/$(id -u) <plist>` and remove it with `launchctl bootout gui/$(id -u)/<label>`.

Never wait for a running job with `pgrep -f <pattern>`. The waiting shell's own command line holds
the pattern, so the wait never ends. Wait on the job's PID, or on a file it writes.

## When a "[...-watch]" prompt arrives

That is the watcher waking you. Do the full check it names and handle it with the user in the
loop. It is a cue, not an order to act.

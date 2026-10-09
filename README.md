# claude-ops

claude-ops is a set of small tools for people who run many Claude Code chats on one Mac at the same time, in different project folders. It finds any chat from any folder, sends a message into a chat that is already running, starts chats inside tmux so they can be reached by name, wakes a chat from a scheduled job when something new happens, and adds a few hooks and skills that came out of daily use.

Everything here started as scripts I wrote for my own machine. I made them general and tested before publishing them.

## Install

```bash
pip install claude-ops
```

It needs Python 3.9 or newer and nothing else from PyPI. Sending into iTerm2 uses AppleScript, so that part works only on macOS. The tmux parts work wherever tmux does.

## Find any chat

Claude Code stores every chat under `~/.claude/projects`, and `claude --resume` only sees the chats of the folder you are in.

```bash
claude-ops sessions                 # all chats from every folder, newest first
claude-ops sessions login           # filter by title, first message, folder or branch
claude-ops search "pg_restore"      # search the full text of every chat (uses ripgrep if it can find it)
```

Each result prints the line that resumes it (`cd <folder> && claude --resume <id>`). The index is cached in `~/.cache/claude-ops/sessions.json` and only changed files are read again, so a later run over twenty thousand chats takes a second or two. The time shown is the last message in the chat, not the file's modification time, because backup and restore tools give every file a new time. A chat is marked "maybe running" when it was active in the last 15 minutes and a `claude` process is running in its folder. Those are two separate facts, so it says "maybe".

## Send a message into a running chat

```bash
claude-ops inject --list                                  # reachable iTerm2 sessions, and which run Claude Code
claude-ops inject --target my-chat --text "please run the tests again"
claude-ops inject --target 0A1B2C3D-1111-2222-3333-444455556666 --file prompt.txt
claude-ops inject --transport tmux --target claude-build --text "..."
```

A target is an iTerm2 session id, part of a tab name, or a tmux session name. Claude Code rewrites tab names, so a job that runs for a long time should keep the session id. Text with several lines is sent as one bracketed paste, so it arrives as one message instead of one message per line.

Before it sends, claude-ops checks that the target is running Claude Code. Text sent into a plain shell would run as a command, so when the check is not sure it refuses (exit code 4). Exit code 3 means the chat is not open.

Every send is written to `~/.local/state/claude-ops/inject.jsonl` before it happens, and its result after, with the same id on both lines. An attempt with no result line means the sender stopped in between. The log keeps the length and a SHA-256 hash of the text, not the text, unless you pass `--keep-text`.

## Start chats inside tmux

```bash
claude-ops chat new build --cwd ~/code/app --trust     # starts "claude -n build" in tmux session claude-build
claude-ops chat show build                             # opens it in an iTerm2 window (tmux -CC)
claude-ops chat end build                              # detaches iTerm2 first, then stops the session
claude-ops chat list
```

A chat in tmux keeps running when its window closes, and other tools can reach it by its tmux name from the first second. A few things I learned while building this.

- The first run in a new folder stops at "Do you trust the files in this folder?". That prompt lists "No, exit" first with the cursor on it, so pressing Enter would close the chat. With `--trust`, claude-ops waits until the screen is still, moves to "Yes, I trust this folder", checks that the cursor is really there, and only then presses Enter.
- If the chat stops at another prompt (sign in, permission mode), `chat new` says which one and how to answer it, instead of waiting until it times out.
- The chat's transcript file appears with the first message, not at launch, so a new chat with no transcript is normal.
- `chat end` detaches iTerm2 before stopping the session, because stopping a session iTerm2 is still attached to leaves an empty window behind.
- Claude Code names its process after its version, so tmux reports the running command as something like `2.1.286`. claude-ops counts that as Claude Code.

## Keep chats current, give them worktrees, move them

These are Python modules, for a script or a scheduler that looks after many chats.

- `claude_ops.config` reads the Claude Code version that is installed and the version each running chat runs, and fingerprints the settings, MCP servers, plugins and CLAUDE.md files a chat loads at start. A fingerprint, because Claude Code rewrites `~/.claude.json` many times an hour.
- `claude_ops.update` says whether a chat may be touched (no command running under it, its turn ended, nobody typing into it, nothing asked on its screen) and restarts a chat in iTerm2 as the same conversation, with the flags and permission mode it had. It ends the process with SIGTERM, never /exit, which can open a dialog.
- `claude_ops.worktree` gives a chat its own git worktree outside the repo, on `chat/<name>`, with the main checkout's local-only files and memory linked in. A folder is reused only by its own branch.
- `claude_ops.move` moves a conversation's files to another project folder so `claude --resume` finds it there. Every copy is checked before an original leaves, and the originals are kept.
- `chat.claude_command` and `chat.resume_command` build the start and resume lines for a named chat with Remote Control. The permission mode is an argument with no default.

Every function that starts a process takes `env`, for callers that build their child environment themselves.

## Wake a chat when something new happens

This is for a job that should bring a chat in to look at something (new issues on a repo, a particular email, a failed build) instead of sending you a notification. A scheduler (launchd, cron or systemd) runs a small script every few minutes, and the script sends one prompt into the chat only when there is something new.

```python
from pathlib import Path
from claude_ops.watch import State, lock, run_source

with lock(Path("/tmp/repo-watch.lock")) as got:
    if got:
        state = State(Path.home() / ".local/state/repo-watch/state.json")
        run_source(state, "issues", fetch=list_issue_ids,
                   describe=lambda new: f"[repo-watch] {len(new)} new issue(s). Please read them and tell me what you think.",
                   target="my-chat")
```

`run_source` handles the parts that went wrong for me in earlier versions.

- The first run records what is already there and sends nothing.
- Items are marked as seen only after the send succeeded. If the chat is closed, the same items are offered again on the next run.
- A source that fails several runs in a row (an expired token, for example) sends one alert into the chat instead of going quiet.
- The lock stops two runs from sending the same thing.

The `chat-watcher` skill explains the full pattern to Claude, including the launchd file.

## Skills

```bash
claude-ops install skills            # copies them to ~/.claude/skills
```

| Skill | What it is for |
|---|---|
| claude-ops | running many chats with these tools, and the traps of iTerm2, tmux and transcripts |
| chat-watcher | building a scheduled watcher that wakes one chat, as above |
| mentor | one chat gives another a task with the standard it must meet, then checks the result itself |
| rover | running something that cuts the Mac off its own wifi, then bringing the wifi back |

## Hooks

```bash
claude-ops install hooks             # prints the settings to add
claude-ops install hooks --write     # adds them to ~/.claude/settings.json and keeps a backup
```

`ask-what-you-need` (Stop hook). I ask my chats to end long reports with a section called "What I need from you". Questions written there as prose were easy to miss, and then they came back in the next report. This hook stops the turn from ending when that section asks for something and Claude did not use AskUserQuestion. The heading can be changed with `CLAUDE_OPS_ASK_HEADING`. It lets the turn end on any unexpected input, and it never blocks the same stop twice.

`context-check` and `context-mark` (PostToolUse and PostCompact hooks). In a long chat, the summary Claude Code writes when the context is full can lose details. These hooks count the tokens written since the last compaction with Anthropic's count_tokens API, and at 85% of the context they tell Claude to write its own structured summary first and then run /compact. They need an Anthropic API key in `~/.claude/anthropic-api-key` (or the file in `CLAUDE_OPS_API_KEY_FILE`). A small chat never calls the API. The limit, the threshold and the /compact prompt are settings, listed at the top of `hooks/context_monitor.py`.

## Playwright profile slots

If you run several Playwright MCP servers, each with its own browser profile, two chats can try to use the same profile and get "Browser is already in use".

```bash
claude-ops slots                     # free, in use (with the browser's pid) or stale
claude-ops slots --first-free        # prints the first free profile name
claude-ops slots --release-stale     # removes locks left by a browser that crashed
```

Chrome marks a profile in use with a `SingletonLock` link whose target ends with the pid of the browser, so the lock itself says whether the profile is taken. The target of that link is a name and not a file, so `os.path.exists` says False even for a held lock. claude-ops reads the link instead.

## Restore chats that disappeared

Claude Code removes old transcripts over time. If another tool keeps copies (the episodic-memory plugin keeps every chat in `~/.config/superpowers/conversation-archive`), this copies back the ones that are missing.

```bash
claude-ops restore --dry-run
claude-ops restore
```

It never overwrites a chat that exists, it keeps each file's original time, and it refuses when more than 200 chats are missing at once (`--max`), because that usually means a wrong path and not lost chats. Set `CLAUDE_OPS_ARCHIVE` for another archive folder.

## Rover

```bash
claude-ops rover path                # where the scripts are
claude-ops rover launch mission.sh   # runs the mission detached, then brings the wifi back
```

Some jobs cut the network the chat reaches the Mac over (joining a device's setup network, changing router settings). The harness runs the mission script with a time limit, then power-cycles the wifi and joins your home network again, and a timer forces that return even if the mission hangs. A separate root watchdog (`netwatchdog.sh`, run by launchd every minute) brings the wifi back if the harness itself dies. The `rover` skill has the install steps and the rules we learned.

## Limits

- iTerm2 sending, the "maybe running" check and the rover scripts are macOS only.
- The check for "is this Claude Code" reads the name of the program in front (node, claude, or a version number). Another node program would also pass, which is why the log exists.
- The inject log is a record, not a security boundary. Any program running as your user can type into your terminals without using claude-ops.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/pytest
```

The tests use temporary chat folders, fake senders and fake lock links, so they need no running chats.

## License

MIT

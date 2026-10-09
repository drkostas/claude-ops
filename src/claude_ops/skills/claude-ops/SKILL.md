---
name: claude-ops
description: >
  Use when you work with other Claude Code chats on the same Mac. That covers finding an old chat
  in any project folder and resuming it, starting a named chat in tmux, sending a message into a
  running chat, handing work to another chat, waiting for a chat to finish its turn, bringing back
  transcripts that disappeared, and sharing Playwright browser profiles between chats. It also
  covers bringing open chats up to date after Claude Code or a CLAUDE.md changed, giving a chat its
  own git worktree, and moving a conversation to another folder. Also use it when a send into
  iTerm2 or tmux behaved strangely, or when a chat received half a message.
---

# claude-ops

Many Claude Code chats can run at once, each in its own project folder, and they often need to
reach each other. This skill is how to do that with the `claude-ops` command without losing a
message, typing into the wrong window or losing a transcript. Every rule below came from a real
failure.

```bash
claude-ops sessions [query]          # every chat from every folder, newest first
claude-ops search "text"             # full text of every chat
claude-ops chat new <name> --cwd <dir> --trust
claude-ops chat show <name>          # reopen its iTerm2 window
claude-ops chat list
claude-ops inject --list [--transport tmux]
claude-ops inject --target <id or name> --text "..."   # or --file, or stdin
claude-ops slots [--first-free | --release-stale]
claude-ops restore [--dry-run]
```

## Find a chat and resume it from the right folder

`claude --resume` only sees the chats of the folder you are in, and a renamed chat is matched by
its title only inside that folder. So a chat started in another project looks lost when it is not.

```bash
claude-ops sessions login            # matches title, first message, folder and branch
claude-ops search "pg_restore"       # when you remember a word, not the title
```

Each result prints the exact line to run, `cd <folder> && claude --resume <id>`. Use the id, not
the title. Two chats can share a title, and then a resume by title stops with an error.

- The time shown is the last message in the transcript, not the file's modification time. Backup
  and restore jobs give every file a fresh time, so a filter on file times once matched almost every
  chat as "changed today".
- "maybe running" joins two separate facts (recent activity, and a `claude` process in that folder).
  A process only shows its folder, and several chats can share one folder, so treat it as a hint.
- `search` uses ripgrep. Inside Claude Code, `rg` in a shell is often a function and not a program,
  so a lookup on PATH finds nothing. claude-ops looks for the real binary itself.
- When you read a transcript for what the user asked, read records of `"type": "queue-operation"`
  as well as `"type": "user"`. A message typed while Claude was still working is stored as a queued
  operation, and a scan of user records alone missed about a quarter of what one user said. The
  asides that get forgotten are exactly those.

## Start a named chat in tmux

A chat in tmux has a fixed name from its first second, survives its window closing, and can be
sent text and read without AppleScript.

```bash
claude-ops chat new build --cwd ~/code/app --trust
claude-ops chat new build --cwd ~/code/app --args "<extra claude arguments>" --no-show
```

This creates tmux session `claude-build`, opens it as an iTerm2 window through `tmux -CC attach`,
then starts `claude -n build`. The window is opened before Claude starts, so its first screen is
drawn into the window.

- The trust prompt lists "No, exit" first with the cursor on it. An answer by position (type 1, or
  press Enter) closes the chat. `--trust` waits until the screen stops changing, moves to "Yes, I
  trust this folder" by reading the words, checks the cursor is on that line, and only then presses
  Enter.
- `--trust` does not answer the permission-mode prompt or a sign-in. Those decide more than one
  folder. `chat new` names the prompt it stopped on and tells you to `tmux attach -t claude-<name>`.
- A new chat has no transcript until its first message. That is a normal start, not a failure.
- Claude Code names its process after its version, so tmux reports the running command as a
  number like `2.1.286`. claude-ops counts that as Claude Code. A check for "node" alone refuses it.
- A tmux window reattached in iTerm2 is blank until the program redraws. `chat show` sends Ctrl+L
  after attaching. Do the same if you attach by hand.
- A tmux server keeps the environment of the terminal that started it and gives it to every pane.
  So every chat in tmux inherited one iTerm2 session id and claimed the same tab. `chat new` starts
  each session with `ITERM_SESSION_ID` cleared. In your own scripts, when `TMUX` is set, never trust
  `ITERM_SESSION_ID`.
- Set iTerm2 to hide the tmux control window and to open tmux windows as normal windows (General,
  then tmux, in its settings). Without that, each attach adds an extra window.

## Send a message safely

```bash
claude-ops inject --list                                  # "claude" marks a reachable chat
claude-ops inject --target <session id> --file prompt.txt
claude-ops inject --transport tmux --target claude-build --text "please run the tests again"
```

Three things decide whether a send works.

1. Multi-line text must be one bracketed paste. Writing text into a terminal types it, and a typed
   newline is Enter. A long handover once reached a chat as many separate messages, and the chat
   answered only the last fragment, saying it contained no request. `inject` wraps multi-line text
   in the paste markers for iTerm2, and uses `tmux paste-buffer -p` for tmux, with a buffer named
   per call so two senders cannot paste each other's text.
2. The target must be running Claude Code. Text pasted into a shell runs as a command. `inject`
   reads the foreground program (iTerm2 shows it in brackets in the tab name, tmux reports it) and
   refuses with exit code 4 when it is not sure. Use `--force` only after you looked yourself.
3. Enter is sent separately, after a short pause (half a second in iTerm2, 0.3 seconds in tmux).
   If Enter arrives while the paste is still being processed, it lands inside the paste and the
   message is not submitted. Use `--no-enter` to paste without submitting.

More rules.

- Hold the iTerm2 session id, not the tab name. Claude Code rewrites tab names, and a tab given a new title
  was still reported to AppleScript under another name. A name match can also hit a
  neighbouring tab.
- Exit code 3 means no such session is open. That is not an error. A sender that is not urgent
  should keep the message and try again later.
- The text is passed to AppleScript as an argument, never written into the script. Older senders
  removed quotes and backslashes so the script would parse, so the chat acted on a different text
  than the one sent. Prove a new sender with a payload holding `"`, `\`, `$` and a newline, on a
  throwaway tab you open and close yourself, never on a chat someone is using.
- Every send writes an attempt line to `~/.local/state/claude-ops/inject.jsonl` before it sends,
  and a result line with the same correlation id after. Read the result line to know it went.
  The log holds a hash of the text unless you pass `--keep-text`, because messages carry secrets.
- The log is a record and not a lock. Any program running as you can type into a terminal without
  it. Never send a chat a text you would not trust it to act on.

## Hand work to another chat

- Name the receiving chat's own folder in the message, not yours. A handover written from one
  folder once described that folder's git state to a chat working somewhere else, and the receiving
  chat acted on the wrong project.
- Give the standard with the task, so the result can be checked. The `mentor` skill covers the
  checking.
- A chat's tools are fixed when it starts. If you added an MCP tool or a skill after the receiving
  chat started, it cannot use it, so tell it to restart or do that step for it.
- Several chats in one git checkout switch branches under each other, and nothing warns. One chat
  committed onto another chat's branch and renamed it. Give each chat its own `git worktree` and
  check `git branch --show-current` before the first commit.
- A worktree is a new folder to Claude Code. Files kept out of git (a local `CLAUDE.md`) are not in
  it, and its memory lives in a separate project folder under `~/.claude/projects`. Copy or link
  both if the chat needs them.
- To resume a chat in another folder, copy its transcript into that folder's project directory
  under `~/.claude/projects` after its process has exited (it keeps writing until then). Move any
  archive copy too, or a restore job brings the old one back. Mark the new folder as trusted first,
  or the resume stops at the trust prompt.

## Wait for a chat to finish its turn

claude-ops has no command for this yet. Two manual ways work.

- A Stop hook that appends one line per finished turn. Claude Code passes the event as JSON on
  stdin, with `session_id` and `transcript_path`. Wait until the line count grows, with a time
  limit.

```json
{"hooks": {"Stop": [{"hooks": [{"type": "command",
  "command": "python3 -c 'import json,sys,time; e=json.load(sys.stdin); print(json.dumps({\"at\": time.time(), \"session\": e.get(\"session_id\")}))' >> ~/.local/state/turn-ends.jsonl"}]}]}}
```

- For a chat in tmux, read its screen with `tmux capture-pane -p -t claude-<name>`. That tells a
  chat waiting at a prompt from one still working, which a transcript alone cannot.

Never wait with `while pgrep -f <pattern>`. `pgrep -f` matches full command lines, and the waiting
shell's own command line contains the pattern, so the loop never ends. Wait on the PID you started
(`kill -0 $P`), or on a file the job writes. `pgrep -f claude` once returned 21 "chats" for one,
because shells, MCP servers and caffeinate all had "claude" in their arguments. Use `pgrep -x claude`.

## Keep transcripts from being lost

Claude Code removes old transcripts. If a tool archives them (the episodic-memory plugin keeps
every chat in `~/.config/superpowers/conversation-archive`), `claude-ops restore` copies back the
missing ones.

```bash
claude-ops restore --dry-run         # what would come back
claude-ops restore                   # refuses above --max (200) at once
```

- It never overwrites a transcript that exists. An earlier restore job also replaced "stale" live
  files with newer archive copies, while the archiver did the same in the other direction. Neither
  kept file times, so the two copied every file back and forth on each run and could replace a chat
  in the middle of a write with a shorter copy. Never add an overwrite based on file times, in
  either direction.
- Copies keep the archive's file time, for the same reason.
- A run that would restore thousands at once means a moved folder or a wrong path, not lost chats.
  Find the cause before you raise `--max`.
- Set `CLAUDE_OPS_ARCHIVE` for another archive folder. Schedule it with launchd or cron.

## Share Playwright profiles between chats

Run several Playwright MCP servers, each with its own profile folder, and let each chat claim one.

```bash
claude-ops slots                     # free, in-use (pid) or stale, per profile
claude-ops slots --first-free
claude-ops slots --release-stale     # only removes locks whose browser pid is dead
```

- Chrome's `SingletonLock` is a symlink whose target ends in the browser's pid. The lock is the
  claim, so no timeouts or flag files are needed. Its target is not a real path, so a plain
  existence check says False for a held lock. Read the link.
- Stay on the profile you claimed for the whole task, and close the browser when done.
- "Target page, context or browser has been closed" means a tab died. Close and navigate again.
  "Browser is already in use" means the profile lock is held. Never kill Chrome by name to fix
  either, because that also kills the other chats' browsers.

## Keep open chats up to date

An open chat keeps the Claude Code version, settings, MCP servers and instructions it started
with. After an update it runs old code until it is restarted, and it does not notice a changed
CLAUDE.md. `claude_ops.config` reads both sides and `claude_ops.update` says when a chat may be
touched and restarts it in place.

```python
from claude_ops import config, update
config.installed_version()                 # what `claude` starts now, e.g. "2.1.294"
procs = config.processes()
config.running_version(pid)                # what one running chat runs (lsof's txt entry)
config.snapshot()                          # fingerprints of settings, MCP servers, plugins, CLAUDE.md
update.idle(pid, procs, update.last_activity(update.transcript_of(chat_id)), turn_ended_at, now)
update.may_be_typing(update.seconds_since_input(), update.front_app())
update.empty_prompt(update.screen_of("iterm", session_id))
update.restart_iterm(pid, session_id, cwd, chat_id)    # same conversation, same flags, same mode
```

- Compare fingerprints, never file times. Claude Code rewrites `~/.claude.json` many times an hour.
- A chat is busy while a shell runs under its claude process (a command or a background task) and
  while it has written since its last turn ended. Without a Stop hook to say when a turn ended,
  wait for ten quiet minutes.
- Read activity from the last message in the transcript, not the file's time. Claude Code appends
  bookkeeping after a turn, and a large transcript can end in megabytes of it, so the read grows
  until it finds a message.
- Never paste into a chat someone may be typing into, or one that shows a question. The terminal in
  front plus input in the last two minutes means wait, and so does anything on screen but an empty
  input line. A paste and Enter would answer a dialog or send their half-typed message.
- Never restart with /exit. It can open Claude Code's own exit dialog and leave a choice on screen.
  `restart_iterm` sends SIGTERM, waits for the shell, and types the resume line.
- A resume loses the permission mode the chat was switched to. `permission_mode` reads it from the
  footer first. `resume_line` keeps the start flags it knows (name, Remote Control, model, the skip
  permission flags) and drops the rest, because `ps` prints a first prompt without its quotes and
  its first word would arrive as a new message.
- `restart_iterm` checks only that Claude is back. Check the new process runs the installed version
  before you call the update done, and wait for the new transcript before sending it a note.

## Give a chat its own worktree

Several chats in one checkout switch branches under each other. `claude_ops.worktree` gives each a
worktree outside the repo, on branch `chat/<name>`, from the remote's default branch.

```python
from claude_ops import worktree
out = worktree.prepare(Path("~/code/app/web"), "fix-login")   # cwd, worktree, linked, memory
worktree.unsafe(Path(out["worktree"]))     # None, or why removing it would lose work
worktree.remove(Path(out["worktree"]))
```

- Files kept out of git by `.git/info/exclude` (a local CLAUDE.md) are linked in, and the chat's
  memory folder is linked to the main checkout's. A pattern in that file is never guessed at.
- Claude Code names a project folder after the resolved path, and older versions kept dots in it,
  so memory is looked for under both spellings and worktree paths have no dots.
- The name is used as given. On a disk that ignores case, `Fix` would find `fix`'s folder, so a
  folder is reused only when its branch is `chat/<name>` and refused otherwise.
- Asked for a subfolder, the chat starts in the same subfolder of its worktree.
- `claude --worktree` puts the worktree inside the repo, which in a synced folder syncs every one.

## Move a conversation to another folder

`claude --resume <id>` finds a conversation only in the project folder of the directory it runs
in. To carry on in another folder, move its files there first.

```python
from claude_ops import move
move.move_files(chat_id, old_project_dir, new_project_dir, archive=archive_root, hold=hold_dir)
```

- Every file is copied first and checked (sha256 for a file, a file count for a folder). Only then
  do the originals leave, so the conversation is never in neither place.
- The originals go to `hold`, never to the bin. If a job keeps a copy of every transcript (a
  conversation archive), pass its root as `archive` so its copies move too and a restore job does
  not bring the old one back.
- Stop the chat before moving it, and resume it in the new folder with `chat.resume_command`.

## Traps

- iTerm2 consent. The first time a new program (a launchd job, a fresh Python) scripts iTerm2,
  macOS asks whether it may control iTerm2. Until a person answers, every AppleScript call that
  lists iTerm2 windows hangs, from any program, including your own shell. Asking iTerm2 for its
  version still answers, so iTerm2 looks fine. The tell is a window count that times out. Never
  click a consent dialog with synthetic input. Ask the user, or use the tmux transport, which needs
  no AppleScript at all.
- Asking iTerm2 anything when it is closed launches it, which from a background job returns empty
  or garbled output. claude-ops asks System Events whether iTerm2 runs first.
- AppleScript says "sent" in its output and exits 0 even when it found nothing. Read the output,
  not only the exit code. Inside a `tell application "iTerm2"` block, `tab` is iTerm2's tab class,
  so join fields with `(ASCII character 9)`.
- Ending a tmux chat. Killing a tmux session or server while iTerm2 is attached with `-CC` leaves
  iTerm2's control windows behind. They are hidden at first and appear later as empty windows.
  Run `tmux detach-client -s claude-<name>` first, then `tmux kill-session -t claude-<name>`. Count
  iTerm2 windows before and after any test, and close a leftover with
  `osascript -e 'tell application "iTerm2" to tell window id <id> to close'`.
- A secret shown in a tmux pane stays in its scroll history. Kill that session when you are done.
- A launchd job gets only `/usr/bin:/bin:/usr/sbin:/sbin` as PATH. A job that calls a Homebrew
  program dies at boot and nothing else notices. Set `EnvironmentVariables` with a full PATH in
  every plist.
- A cleanup job whose failure is silent looks like one that works. One reaper failed on every run
  because macOS `ps` has no `etimes` column, and its log held only that error. Make every
  scheduled job check that it did something, and read its log once after installing it.

## Hooks

```bash
claude-ops install hooks             # prints the settings
claude-ops install hooks --write     # writes them, keeps a backup of settings.json
```

`ask-what-you-need` (Stop) blocks the end of a turn when a "What I need from you" section asks for
something and no AskUserQuestion was called. It lets the turn end on any unexpected input and never
blocks the same stop twice. `context-check` and `context-mark` (PostToolUse, PostCompact) warn a
long chat before its context fills, so it writes its own summary before `/compact`.

## Related skills

- `chat-watcher` for a scheduled job that wakes a chat when something new happens.
- `mentor` for directing another chat and checking its work.
- `rover` for jobs that cut the network the Mac is reached over.

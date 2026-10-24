---
name: claude-ops
description: >
  Use when you work with other Claude Code chats on the same Mac. That covers finding an old chat
  in any project folder and resuming it, starting a named chat in tmux, sending a message into a
  running chat, handing work to another chat, waiting for a chat to finish its turn or noticing
  that a background agent stalled, bringing back
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
claude-ops stall <transcript or folder> [--minutes 30] [--max-hours 12] [--done-file F]
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

## Read what the person asked, and what was said around it

Before you answer "what did they ask for", read their messages, not your memory of them.
`claude-ops messages` lists what the person typed, oldest first, from any chats and any range, and
`--around` shows the conversation before and after one message.

```bash
claude-ops messages --since 7d --chat build              # titles take globs; --folder and --session too
claude-ops messages --since 2026-01-05 --until 2026-01-06 --grep "deploy|backup"
claude-ops messages --around <session>:<line> --before 6 --after 12 --tools
```

- Never read only "user" records. A message typed while Claude works is a queue-operation first. It
  may then arrive as a `queued_command` attachment inside the running turn and never as a user
  record. Those asides are the requests most often forgotten. `messages` reads all three shapes and
  folds the copies of one message into one entry.
- A "user" record is often not the person. Task notifications, hook feedback, skill text,
  compaction summaries, schedules and other sessions all write them. So does a program that types
  into the terminal. `messages` drops a record by its own flags (`isMeta`, `origin`, `turnOrigin`,
  `scheduledTaskId`) and by the text's opening.
- A message that opens with a bracketed tag (`[watcher] ...`), or a paste whose every part does, is
  treated as a program's. Pass your own pattern as `program_tag` when your tools type differently.
- A queued message's copies arrive within hours of each other. The same words typed again later are
  a new message.
- To check that a request was done, read `--around` it, with `--tools` when you need to see what
  was run. Then check the result itself (the merge, the file, the running service), not the chat's
  account of it.

## Send a message safely

```bash
claude-ops inject --list                                  # "claude" marks a reachable chat
claude-ops inject --target <session id> --file prompt.txt
claude-ops inject --transport tmux --target claude-build --text "please run the tests again"
```

Four things decide whether a send works.

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
4. The chat must show its empty input line. A paste joins whatever a person has half-typed there,
   and the Enter sends both as one message; a question on screen takes the Enter as its answer.
   `inject` reads the screen first and answers `not-ready` (exit code 5) for anything else, so a
   watcher keeps the message and tries again. The suggested next prompt Claude Code draws dim in
   an empty input line is not typed text, and `update.plain_screen` drops it from a tmux capture.

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

To wait on one session's transcript (a background agent you started, or a chat you handed work
to), run `claude-ops stall` on it as a background command next to the work. It checks once a
minute and exits when something happened, so the background command's end is the signal.

```bash
claude-ops stall ~/.claude/projects/<folder>/<chat id>/subagents/agent-<id>.jsonl --minutes 30
claude-ops stall ~/.claude/projects/<folder>/<chat id>/subagents          # every agent of a chat
claude-ops stall /path/to/output --done-file /path/to/output/DONE        # a job that writes a file
```

| Result | Exit | Meaning |
|---|---|---|
| finished | 0 | the newest message is the assistant's with stop_reason end_turn (for a folder, every transcript in it) |
| done | 0 | the `--done-file` exists and is not empty |
| gone | 0 | the path no longer exists |
| error | 1 | the newest message is Claude Code's record of an API error (a usage limit, for example), and the session waits |
| stalled | 1 | nothing under the path changed for `--minutes`, and the line says what the last record was |
| timeout | 2 | still changing after `--max-hours` |

- The end of a turn is read from the newest message record, not the last line. Claude Code appends
  bookkeeping records after a turn ends, so the last line is often not a message.
- A stall line names what the session waits on ("a tool call with no result", "a tool result with
  no answer"). A tool call with no result for a long time is often a command that hangs.
- For a folder, "finished" needs every transcript in it to have ended. Agents that start in phases
  can all be finished between two phases, so for a workflow wait on a file it writes at the end
  with `--done-file`.
- In Python it is `claude_ops.watch.stall(path, minutes=30, max_hours=12, done_file=None,
  poll_s=60)`, and `stall_check(path, minutes, done_file, now)` looks once, for a scheduled job.

For a chat's own end of turn, two more ways work.

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
update.ready(pid, procs, chat_id, update.screen_of(kind, handle), turn_ended_at, now)  # all of the checks above
update.restart_iterm(pid, session_id, cwd, chat_id)    # same conversation, same flags, same mode
update.restart_tmux(pid, "claude-<name>", chat_id)     # the same, inside the chat's own tmux pane
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
- A chat is also busy while a background agent it started writes (`background_agent`: a file
  under `<chat id>/subagents/` written in the last two minutes). The agent runs inside the chat's
  process and dies with it, and it never writes to the chat's own transcript.
- A chat waiting for an answer to a question has not ended its turn. `ready` says so in those
  words, rather than "working", so whoever reads the reason knows the person is the one to act.
- `restart_tmux` restarts a tmux chat inside its own pane: SIGTERM, wait for the pane's shell, type
  the resume line there. Never end the session and make a new one: that opens a new window for a
  chat the person had in a tab of their choosing.
- `restart_iterm` checks only that Claude is back. Check the new process runs the installed version
  before you call the update done, and wait for the new transcript before sending it a note.
- A restart does not refresh the MCP tools a chat already loaded. Claude Code writes each deferred
  tool's definition into the transcript when the chat first loads it, and a resumed conversation
  keeps using those until it is compacted, so an argument the server gained since then cannot be
  passed and its new description is not seen. `update.tools_held(transcript)` lists the definitions
  the chat holds. Compare them with the server's tools, and tell the chat which arguments it cannot
  pass yet and how else to reach them (a command-line twin, for example).

## Move a running chat into tmux, inside its own tab

A chat started in an iTerm2 tab can move into tmux without a new window: it is a restart whose
last line starts a session and attaches the same tab to it.

```python
from claude_ops import chat, update
update.restart_into_tmux(pid, iterm_session_id, cwd, chat_id, title="vision")
# (True, {"tmux": "claude-vision", "tab": "...", "shown": "plain tmux"}) or (False, {"step", "why"})
chat.in_tmux(pid, procs)          # is a process inside a tmux pane (from the process table)
chat.server_context()             # "none", "iterm", "outside" or "unknown"
```

- Check `ready` and `may_be_typing` first. A background command or agent dies with the process,
  so a chat with one waits.
- The session must have iTerm2's context. Everything in a tmux server runs in the context of
  whatever started the server, and one started by a launchd job cannot script iTerm2, so the
  chat would lose iTerm2's permissions. `restart_into_tmux` asks `server_context` and refuses
  "outside". With no server running, the line typed into the tab starts one there, inside iTerm2.
- Plain tmux stays in the tab. iTerm2's tmux mode (`-CC`) shows the session as a new tab or
  window and leaves the old tab behind, so it is not the default.
- `chat.plain_look` makes the tab look and behave as before: no status bar, the mouse wheel
  scrolls tmux's history, and the pane's title (the chat's name, set by Claude Code) is passed to
  the tab. Without set-titles every moved tab read "cd (tmux)".
- Remote Control comes back named after the chat (`title_of` reads the chat's own title).

## Give a Remote Control session a new title

A Remote Control session keeps the title it was first made with. Resuming the chat reattaches it to
that session, and neither `claude --remote-control <name>` nor `/rename` changes the title that
claude.ai and the phone app show. `claude_ops.remote.set_title` changes it through the sessions API
Claude Code itself uses, with the login of the account that owns the session, and reads it back. No
browser has to be signed in to that account.

```python
from claude_ops import accounts, remote
s = remote.remote_session(transcript)          # {"row", "account"}: the session, and whose it is
items = [(accounts.LIVE_SERVICE, "<mac user>"), ("my-saved-login-b", "<mac user>")]
login = accounts.login_for(s["account"], items)
token = login["claudeAiOauth"]["accessToken"]
remote.title(s["row"], token)                  # what claude.ai shows now
remote.set_title(s["row"], "build", token)     # {"ok", "title", "changed"}, or {"ok": False, "why"}
```

- Use the token of the account that owns the session. A login that cannot see the session
  (another account's, or a session that is gone) answers `remote.NOT_FOUND`, and an expired or
  revoked login answers `remote.REFUSED`.
- `set_title` reads the title first and sends nothing when it already matches, then reads it back
  after the change. Report the title it read back, not the one you sent.
- A restart can make a new session with a title of its own, so keep the session id with the title
  you set, and set it again when the id changes.
- Stop after a few failures for one session. A session that is never found stays not found.
- Never print or log the token. Read it inside the script that makes the call.
- The sessions API is not documented. If Anthropic changes it, `set_title` fails with a reason and
  the chat itself keeps working.

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

## Merge a pull request only when it is green

When a chat merges a pull request, it runs `claude-ops merge <number>` (with `--repo owner/name`
outside the repository's folder) instead of waiting for checks and then calling `gh pr merge`. Two
separate steps can merge a pull request whose checks went red in between, or whose failure was cut
from a shortened log. The command waits for every check on the current head, refuses any that did
not pass and a pull request with no checks, and merges that exact head with `--match-head-commit`.
Read its exit code: 0 merged, 2 still running or no checks, 3 a check failed (its names are
printed), 4 the merge was refused, 5 not open.

## Repository tools: union, secret-shapes, mutate, plan-apply

- **A rebase stopped on a conflict where both sides only added lines** (two branches adding an entry
  at the same place): `claude-ops union <worktree>`, after rebasing with
  `git -c merge.conflictStyle=diff3 rebase`. It stages what it settled; exit 1 means a real conflict,
  so abort the rebase and resolve it by hand. Name files where each entry appears once with
  `--ordered`, and append-only logs with `--journal`.
- **Before pushing to a repository whose pull requests a secret scanner reads**:
  `claude-ops secret-shapes <worktree> <base> --shapes <file>`, or `--pre-push` in a pre-push hook.
  When the scanner flags a line that holds no secret, add its shape to the file with the fix that
  cleared it, so the next push is refused before the scanner sees it. Rewrite the commit that added
  the line; a later commit that removes it does not help.
- **To prove a test can fail**: `claude-ops mutate <worktree> --run "<command with {copy}>"
  --function <file>:<name>`. Every mutation the command lets pass is a gap in the tests. `--list`
  shows the mutations without running anything; `--caught` names the exit codes that mean a test
  failed when the runner uses more than 1.
- **To execute a written plan's file steps**: `claude-ops plan-apply PLAN.md '<words from the step
  header>'`. Name steps by words rather than line numbers when the plan may still change.

## Heavy jobs: one at a time, in turn, under a memory cap

Run anything that takes a lot of memory, CPU or disk (an Android or Xcode build, an emulator, a
render or mesh job, a local model, a crawl, a scan of a large tree) through
`claude-ops heavy <chat> "<what>" -- <command...>`. It waits for room and for its chat's turn, ends a
job whose process group passes `HEAVY_MAX_GB` (8 by default), and says the job's peak memory when it
ends, so the next run can set the cap it needs. Set `HEAVY_MAX_GB` on the same line for a job that
needs more. Give a job that should end by itself `--max-hold <minutes>`. With `--notify` and a
`HEAVY_NOTIFY` command, the result reaches the chat instead of a polling loop. `claude-ops heavy
status` says who holds the slot and who waits, and why. A job that fails the same way three times in a row (`HEAVY_REPEAT_FAILS`)
says so with `AGAIN:`, and `status` lists it under "failing the same way": fix the cause (often the
cap it needs) before it runs again.

## Tell chats apart in a shared window by tab color

```bash
claude-ops tab-colors                # needs pip install 'claude-ops[iterm]' and iTerm2's Python API on
```

In every iTerm2 window that holds two or more chats, each tab gets a color no other tab there has.
A color the user set is kept. A supervisor that runs it on a schedule can pass `remembered` to
`iterm.assign` (or `iterm.color_chat_tabs`) so a chat keeps its color after a restart or a move.

- A session object read through iTerm2's API belongs to that connection, and writing to it after
  the connection closed fails. `set_tab_color` finds the session by its id in a connection of its own.
- iTerm2 reports colors as floats (234.4, 116.1, 103.6). Compare them rounded.

## Find a chat's iTerm2 window, and tidy the empty ones

```python
from claude_ops import iterm
iterm.window_of_session(os.environ["ITERM_SESSION_ID"])     # the window a session is in, or None
iterm.window_of_ttys(iterm.tmux_client_ttys("claude-build")) # a tmux chat's window, by terminal
iterm.session_of_tmux_pane(os.environ["TMUX_PANE"])          # "tmux:<UUID>" for a tmux -CC pane
iterm.windows()                                              # id, tab count, frame, title
iterm.dead_windows()                                         # frames iTerm2 closed but left on screen
iterm.close_empty_window(window_id)                          # "closed", "asked", "kept", "not found"
```

- A process inside tmux has no ITERM_SESSION_ID of its own. Find its window by its tmux client's
  terminal, or by the pane through `session_of_tmux_pane`. Never by the window title: it names only
  the front tab.
- Each window is read inside its own `try`. A window iTerm2 closed can stay in its list,
  unreadable, and a listing without the `try` failed on every call while it was there.
- `close_empty_window` closes a window only when it holds no session (a leftover tmux control
  window). A window with a session is never closed, because that would detach the chat in it.
- `set_bounds(window_id, frame)` sizes a window through iTerm2 itself, for a window yabai has
  lost. It fits `claude_human.desktops.frame_window(..., fallback=...)`.
- An answer that could not be read is `None`, never "no windows".

## Say what a deploy brought

When something you run moves a checkout to a new commit (a deploy, an update of a tool), tell the person which pull requests arrived, not the two hashes: `claude-ops changelog <old> <new> --repo <checkout>`. It walks the first-parent history, so each pull request is one line ("#12 Retry on a timeout"), and a commit pushed straight to the branch is listed by its subject. Take `old` from before the move, never from the remote, or the list is empty.

## Read the tracebacks a service logged

When something on the machine "works but misbehaves", or before saying a service is healthy, read its log's tracebacks rather than the last few lines: `claude-ops tracebacks <log> --prefer "*/<your package>/*" --ignore BrokenPipeError`. The groups say which code fails and how often. A watcher that runs on a schedule keeps `read_new`'s next offset between passes and starts a new log at its end (the history is not news). Client disconnects (`BrokenPipeError`, `ConnectionResetError`) are a peer going away, not a bug in the service.

## Traps

- iTerm2 consent. The first time a new program (a launchd job, a fresh Python) scripts iTerm2,
  macOS asks whether it may control iTerm2. Until a person answers, every AppleScript call that
  lists iTerm2 windows hangs, from any program, including your own shell. Asking iTerm2 for its
  version still answers, so iTerm2 looks fine. The tell is a window count that times out. Never
  click a consent dialog with synthetic input. Ask the user, or use the tmux transport, which needs
  no AppleScript at all.
- Asking iTerm2 anything when it is closed launches it, which from a background job returns empty
  or garbled output. claude-ops asks System Events whether iTerm2 runs first.
- Loop over `(get windows)`, `(get tabs of w)` and `(get sessions of t)`, never over the bare
  collections. A bare loop variable is a position, read again on every use, and Brave and Chrome
  list their windows front to back, so a window brought forward in the middle of a loop makes
  `id of w` and `tabs of w` name two different windows. With `get`, the loop holds "window id"
  and "session id" references: a write reaches the session that matched, or fails.
- A read can crash iTerm2. `get (variable named "...") of every session of every tab of every
  window`, one request over every session, ended iTerm2 3.6.10 with an uncaught exception in its
  AppleScript handler, and every window closed. Read a variable one session at a time, inside
  `try`. iTerm2's session server keeps the shells, so relaunching iTerm2 brings the windows back,
  but all of them on one desktop, and the relaunched iTerm2 asks again for consent to script it.
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

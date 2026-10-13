# Changelog

## Unreleased

- `update.unfinished_background(transcript)`: the background commands a chat started that never got a task notification, with their command text, so a chat resumed after a restart can start again what it still needs.
- `inject` sends only into an empty input line: a half-typed message, a question on screen or a screen it cannot read answers `not-ready` (exit code 5) and sends nothing. `--force` still skips the checks
- `update.screen_of` reads a tmux pane with its styles and `update.plain_screen` drops the suggested next prompt Claude Code draws dim in an empty input line, so `empty_prompt` no longer reads a suggestion as text somebody typed. `chat.pane_text(styled=True)` keeps the codes, and `update.TYPED` names the typed-text answer
- `update.resume_line(..., name=...)`: the chat's current name replaces the `-n`/`--name` (and a named `--remote-control`) it started with, so a restart does not undo a rename.
- `update.model_of` reads the model and effort level a chat last ran on from its transcript, and every restart and `chat.resume_command` pass them on (`--model`, `--effort`), so a model or effort chosen inside the chat survives a restart. A chat started with `--settings` keeps the model its settings file names
- `update.restart_into_tmux` moves a chat from an iTerm2 tab into tmux shown in that same tab, and `update.restart_tmux` restarts a tmux chat inside its own pane. Neither opens a window
- `update.ready` puts every check on a chat in one place, adds `update.background_agent` (a background agent keeps a chat busy), and names a question waiting for the person instead of calling the chat "working"
- `update.remote_control_of` and `update.title_of` read a chat's Remote Control state and title from its transcript, so a restart keeps Remote Control
- `update.front_app(bundle_id=True)` gives the bundle id of the app in front
- `config.descendants(pid, procs)`, every process under one, so a window can be traced to the chat whose process made it
- `chat.in_tmux`, `chat.plain_look`, `chat.attach_line` and `chat.server_context`. A plain tmux session passes the chat's title to the terminal tab, so the tab no longer reads "cd (tmux)"
- `chat.claude_command` and `chat.resume_command` take `settings`, a settings file for that chat alone (`--settings <file>`), and a restart keeps the flag

## 0.4.0

- `claude_ops.config`: the installed Claude Code version, the version a running chat runs, and fingerprints of what a chat loads at start
- `claude_ops.update`: whether a chat may be touched, and `restart_iterm`, which restarts a chat in iTerm2 as the same conversation with its flags and permission mode
- `claude_ops.worktree`: a chat's own git worktree with the main checkout's local files and memory linked in
- `claude_ops.move`: move a conversation's files to another project folder, checked before any original leaves
- `chat.claude_command`, `chat.resume_command`, `chat.pane_owner`, `chat.at_prompt`, `chat.repo_name`, and `sessions.rc_url`, `sessions.pick`, `sessions.project_for`
- The skill covers all of it

## 0.3.0

- Every function that starts a process (tmux, osascript, pgrep, lsof, rg) takes an optional `env`, passed to the process. `None` keeps the inherited environment

## 0.2.0

- A main `claude-ops` skill, and more detail in chat-watcher, mentor and rover from the conversations they came from
- `claude-ops chat end`, which detaches iTerm2 before stopping the tmux session
- The rover harness no longer needs `timeout`, which stock macOS does not have

## 0.1.0

First release.

- `claude-ops sessions` and `search` find chats from every project folder
- `claude-ops inject` sends a message into a running chat in iTerm2 or tmux, with an append-only log
- `claude-ops chat` starts chats in tmux, answers the trust prompt by its words, and shows them in iTerm2
- `claude_ops.watch` for scheduled watchers that wake a chat only when something is new
- Skills chat-watcher, mentor and rover
- Hooks ask-what-you-need, context-check and context-mark
- `claude-ops slots` for Playwright profile locks, and `claude-ops restore` for chats that disappeared

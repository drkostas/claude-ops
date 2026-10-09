# Changelog

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

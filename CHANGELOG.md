# Changelog

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

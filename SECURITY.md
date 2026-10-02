# Security

`claude-ops inject` types text into terminals, and the hooks read your chat transcripts. Please report a security problem privately through GitHub's "Report a vulnerability" button on this repository, not in a public issue.

Some things to know before you use it.

- A message sent into a chat is acted on with that chat's permissions. Do not send text from an untrusted source into a chat that can run commands.
- The check that a target runs Claude Code protects against sending into a plain shell by mistake. It is not a security boundary.
- The inject log stores a hash of each message, not the text, unless you pass `--keep-text`. Messages can carry secrets, so keep it off unless you need it.
- The context hooks send the recent text of a chat to Anthropic's count_tokens API with your own API key.

# Contributing

Issues and pull requests are welcome. Claude Code changes its screens and its process names from one version to the next, so reports of a prompt or a name that claude-ops does not recognise are very useful.

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/pytest
```

Please add a test for every change in behaviour. If a change is about what Claude Code shows on screen, please include the pane text (`tmux capture-pane -p -t <session>`) in the issue or the test, and your Claude Code version.

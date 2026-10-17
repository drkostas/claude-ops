# Contributing

Issues and pull requests are welcome. Claude Code changes its screens and its process names from one version to the next, so reports of a prompt or a name that claude-ops does not recognise are very useful.

```bash
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/pytest
```

Please add a test for every change in behaviour. If a change is about what Claude Code shows on screen, please include the pane text (`tmux capture-pane -p -t <session>`) in the issue or the test, and your Claude Code version.

Two rules keep the tests honest on a busy machine and beside another installed copy:

- A test that starts `python -m claude_ops...` in a child process sets `PYTHONPATH` to the package
  under test. Otherwise the child imports whichever claude-ops that interpreter has installed.
- A check that needs a job still running makes the job wait for a file the test creates afterwards,
  never for a fixed sleep: a slow machine finishes the sleep before the check runs.

## Writing for this repository

Everything here is public, including commit messages and pull request descriptions. Please write them for a user of the package. Do not name other projects, people or machines that use it, and keep real hostnames, IP addresses, paths and credentials out of code, tests and history (examples use `example.org` and `192.0.2.x`). A check on every pull request looks for terms that must not appear here.

"""The environment a child process runs with: declared, never inherited.

An automation that starts commands usually hands each child the whole environment of whoever started
it. Then any exported variable can change what the child does, and some do much more than inform it:

    PYTHONPATH=/tmp/x python3 -c "import x"   imports a module from a folder the caller chose
    GIT_DIR=/tmp/other git log                beats the working directory, so a push can go elsewhere

`child_env` builds the environment from nothing and adds only what is named: a short list of
variables a command legitimately needs (`BASE`, each with its reason), the ones the caller keeps or
sets at the call site, and the PATH, which the caller must always give. A variable nobody thought of
is left out because the list never included it, not because someone predicted it.

`REDIRECTS` names variables that redirect a command rather than inform it. The allowlist already
leaves them out; they are named so a reader can see what the guard is for, and so that keeping or
setting one is refused outright.

This is not a sandbox. A child can still read files, reach the network and do whatever its binary
does. It removes one way of influencing it: through the environment instead of its arguments.

    env = child_env(path="/usr/bin:/bin")
    env = child_env(path="/usr/bin:/bin", keep=("DATABASE_URL",), add={"LANG": "C.UTF-8"})
"""
from __future__ import annotations

import os

#: What a command legitimately needs, and nothing else. Each entry is a decision, with its reason.
BASE: dict[str, str] = {
    "PATH": "which binaries exist. Always given by the caller rather than inherited: a scheduled job "
            "gets whatever its scheduler sets, which is often too little",
    "HOME": "git, ssh and adb read their configuration from it, and act as another user without it",
    "USER": "some tools identify the caller from it",
    "LOGNAME": "the same, for tools that read the other one",
    "LANG": "how text is decoded. Without it, output arrives in an encoding the parser did not expect",
    "LC_ALL": "the same, and it wins over LANG",
    "LC_CTYPE": "the character set. Set to UTF-8 when no locale is named at all, because a scheduled "
                "job often has none, and some tools then print a tab or a non-ASCII character as `_`",
    "TMPDIR": "where a child may write scratch files",
    "TERM": "a pseudo-terminal needs it",
    "SSH_AUTH_SOCK": "the key agent, without which ssh cannot use the user's keys",
}

#: Variables that redirect a command rather than inform it. Never kept or added: `child_env` refuses
#: them. They are absent anyway, because BASE does not list them.
REDIRECTS: dict[str, str] = {
    "PYTHONPATH": "makes a child Python import code from a folder the caller chose",
    "PYTHONHOME": "relocates the whole interpreter",
    "PYTHONSTARTUP": "runs a file before anything else",
    "GIT_DIR": "points git at another repository, and beats the working directory",
    "GIT_WORK_TREE": "the same, for the tree rather than the metadata",
    "GIT_SSH_COMMAND": "replaces the program git reaches a remote with",
    "GIT_CONFIG_GLOBAL": "replaces the identity and the remote rewriting rules",
    "ANDROID_SERIAL": "sends a bare adb command to another device (adb -s still wins)",
    "LD_PRELOAD": "loads a library into the child before its own code",
    "LD_LIBRARY_PATH": "the same, by search path",
    "DYLD_INSERT_LIBRARIES": "the macOS form",
    "DYLD_LIBRARY_PATH": "the macOS search-path form",
    "NODE_OPTIONS": "injects flags, including a module to require, into any Node child",
    "TMUX": "makes a tmux command act on the server the caller is attached to, not the one named",
}

#: the character set given when the caller names no locale (BASE, LC_CTYPE)
UTF8 = "en_US.UTF-8"


def child_env(*, path: str, add: dict[str, str] | None = None, keep: tuple[str, ...] = (),
              environ: dict[str, str] | None = None) -> dict[str, str]:
    """The environment to start a child with, built from nothing.

    `path` is required: the caller knows which binaries its command needs. `keep` carries named
    variables over from the current environment when they are set (a test pointing a child at a
    scratch database, say), and `add` sets values outright. A name in either that is in REDIRECTS
    raises ValueError. `environ` is the environment to read from (os.environ when not given)."""
    for name in (*keep, *(add or {})):
        if name in REDIRECTS:
            raise ValueError(f"{name} redirects the command it is given to ({REDIRECTS[name]}); "
                             "it cannot be passed to a child")
    source = os.environ if environ is None else environ
    env: dict[str, str] = {"PATH": path}
    for name in (*BASE, *keep):
        if name != "PATH" and source.get(name) is not None:
            env[name] = source[name]
    if not any(env.get(n) for n in ("LC_ALL", "LC_CTYPE", "LANG")):
        env["LC_CTYPE"] = UTF8
    env.update(add or {})
    return env

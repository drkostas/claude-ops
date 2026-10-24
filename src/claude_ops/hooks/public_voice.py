"""Keep private names out of public repositories (a PreToolUse hook).

A person who keeps private projects beside public ones has words that must never reach the public
side: the names of private projects and machines, internal paths and addresses, personal details.
A model writing a commit, a README or a pull request does not always remember which repository is
public, so this hook refuses the tool call instead (exit 2, with the reason sent back to the model).

The words are regular expressions in a JSON file the user keeps outside every repository. The hook
checks text only when its target repository is public on GitHub (asked with `gh repo view`, and
cached), and it checks:

- the content of Write, Edit and MultiEdit calls for a file inside a public repository;
- a `git commit` or `git tag` message (`-m`, `--message`, `-F`, `--file`) and a `gh pr`, `gh issue`
  or `gh release` title, body or notes (and the body or notes files), on any Bash command line;
- what a `git commit` would record: the staged changes, the tracked changes for `-a`, what a
  `git add` on the same line would stage and the paths the commit names, with the file names
  (read through `commandline.commit_lines`, which follows `git -C`, `cd` and the like);
- text a Bash command writes into a file inside a public repository: a here-document given to a
  command whose output goes to the file or to `tee`, and `echo` or `printf` redirected to it;
- the text arguments of a GitHub MCP call for a public owner/repo.

The configuration (all keys optional; a missing file means no terms, so nothing is refused):

    {
      "terms": ["\\\\bmy-private-project\\\\b", "internal\\\\.example\\\\.org"],
      "terms_except_personal": ["\\\\bjane\\\\b"],
      "personal_repositories": ["jane/jane.github.io"],
      "allow": ["github\\\\.com/jane/[A-Za-z0-9_.-]+"],
      "term_files": [{"path": "~/.config/me/more-terms.json", "key": "terms"},
                     {"path": "~/.config/me/more-terms.json", "key": "home", "except_personal": true}],
      "visibility_cache": "~/.cache/claude-ops/public-voice-visibility.json",
      "message": "Write for a reader who has never seen the private projects."
    }

`terms` are refused in every public repository. `terms_except_personal` are refused everywhere but
in the `personal_repositories`, which are about the person and carry their name on purpose.
`allow` patterns are removed from the text before the terms are matched. `term_files` adds the list
under `key` in another JSON file, read on each run, so a list another program keeps current needs
no change here. Matching ignores case. `message` is added to every refusal.

    python -m claude_ops.hooks.public_voice [--config FILE]     # reads the tool call on stdin
    python -m claude_ops.hooks.public_voice [--config FILE] --terms | --terms-for OWNER/REPO | --allow

`--terms` prints every term, `--terms-for` the terms that apply to one repository and `--allow` the
allow list, one per line, for a CI check that reads the same lists from a secret. The configuration
is `--config`, else `CLAUDE_OPS_PUBLIC_VOICE_CONFIG`, else `~/.config/claude-ops/public-voice.json`.
On anything unexpected the call goes through: a broken hook must never stop work.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

from .commandline import (
    READ_LIMIT, commit_plan, commit_repos, expand, git_invocations, hidden_commit, recorded,
    steps, writes,
)

DEFAULT_CONFIG = "~/.config/claude-ops/public-voice.json"
CONFIG_ENV = "CLAUDE_OPS_PUBLIC_VOICE_CONFIG"
DEFAULT_CACHE = "~/.cache/claude-ops/public-voice-visibility.json"
#: how long a repository's visibility is trusted before it is asked again
TTL_S = 6 * 3600
#: `gh` options whose value is published text, and those that name a file holding it
GH_TEXT_FLAGS = {"--title", "-t", "--body", "-b", "--notes", "-n", "--message", "-m"}
GH_FILE_FLAGS = {"--body-file", "-F", "--notes-file"}
#: `git commit|tag` options that name a message file
GIT_FILE_FLAGS = {"-F", "--file"}
#: keys of a GitHub MCP call that name its target rather than carry text
GITHUB_TARGET_KEYS = {"owner", "repo", "path", "branch", "sha"}


class Config:
    """The lists from the configuration file."""

    def __init__(self, data: dict | None = None):
        data = data if isinstance(data, dict) else {}

        def strs(key: str) -> list[str]:
            v = data.get(key) or []
            return [s for s in v if isinstance(s, str) and s] if isinstance(v, list) else []

        self.terms = strs("terms")
        self.terms_except_personal = strs("terms_except_personal")
        self.personal = {s.lower() for s in strs("personal_repositories")}
        self.allow = strs("allow")
        files = data.get("term_files") or []
        self.term_files = [f for f in files if isinstance(f, dict) and isinstance(f.get("path"), str)] \
            if isinstance(files, list) else []
        cache = data.get("visibility_cache")
        self.visibility_cache = os.path.expanduser(cache if isinstance(cache, str) and cache else DEFAULT_CACHE)
        message = data.get("message")
        self.message = message if isinstance(message, str) else ""

    def file_terms(self, except_personal: bool) -> list[str]:
        out: list[str] = []
        for f in self.term_files:
            if bool(f.get("except_personal")) != except_personal:
                continue
            try:
                with open(os.path.expanduser(f["path"])) as fh:
                    got = json.load(fh).get(f.get("key") or "terms", [])
            except Exception:  # noqa: BLE001 - a list that cannot be read adds no terms
                continue
            out += [t for t in got if isinstance(t, str) and t] if isinstance(got, list) else []
        return out

    def terms_for(self, personal: bool) -> list[str]:
        """Every term that applies to a public repository, which is personal or not."""
        out = self.terms + self.file_terms(False)
        if not personal:
            out += self.file_terms(True) + self.terms_except_personal
        return out

    def is_personal(self, slug: str | None) -> bool:
        return bool(slug) and slug.lower() in self.personal


def config_path(argv_path: str | None = None) -> str:
    return os.path.expanduser(argv_path or os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG)


def load(path: str) -> Config:
    """The configuration in this file; a missing or unreadable file is an empty one."""
    try:
        with open(path) as f:
            return Config(json.load(f))
    except Exception:  # noqa: BLE001 - no configuration, no terms
        return Config()


_compiled: dict[str, re.Pattern | None] = {}


def _pattern(p: str) -> re.Pattern | None:
    if p not in _compiled:
        try:
            _compiled[p] = re.compile(p, re.I)
        except re.error:
            _compiled[p] = None           # a pattern that does not compile matches nothing
    return _compiled[p]


def offending(text: str, terms: list[str], allow: list[str]) -> str | None:
    """The first text in TEXT that a term matches, after the allowed text is removed, or None."""
    if not text:
        return None
    clean = text
    for a in allow:
        pat = _pattern(a)
        if pat:
            clean = pat.sub("", clean)
    for t in terms:
        pat = _pattern(t)
        m = pat.search(clean) if pat else None
        if m:
            return m.group(0)
    return None


# --- which repository, and is it public ----------------------------------------------------------

def slug_of(directory: str) -> str | None:
    """owner/repo of the GitHub `origin` remote of the git work tree holding `directory`."""
    d = directory
    while d and not os.path.isdir(d):
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    if not d:
        return None
    try:
        r = subprocess.run(["git", "-C", d, "remote", "get-url", "origin"], capture_output=True,
                           text=True, timeout=4)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(\.git)?$", r.stdout.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def repo_visibility(slug: str) -> str | None:
    """PUBLIC, PRIVATE or INTERNAL as GitHub answers for owner/repo, or None when it did not."""
    try:
        r = subprocess.run(["gh", "repo", "view", slug, "--json", "visibility", "-q", ".visibility"],
                           capture_output=True, text=True, timeout=6)
    except (OSError, subprocess.SubprocessError):
        return None
    vis = r.stdout.strip()
    return vis if vis in ("PUBLIC", "PRIVATE", "INTERNAL") else None


def is_public(slug: str | None, cfg: Config) -> bool:
    """owner/repo is public on GitHub. Cached for TTL_S; an unknown answer is False (fail open)."""
    if not slug:
        return False
    try:
        with open(cfg.visibility_cache) as f:
            cache = json.load(f)
        if not isinstance(cache, dict):
            cache = {}
    except (OSError, ValueError):
        cache = {}
    hit = cache.get(slug)
    if isinstance(hit, list) and len(hit) == 2 and time.time() - hit[1] < TTL_S:
        return hit[0] == "PUBLIC"
    vis = repo_visibility(slug)
    if vis is None:
        return False
    cache[slug] = [vis, time.time()]
    try:
        os.makedirs(os.path.dirname(cfg.visibility_cache), exist_ok=True)
        with open(cfg.visibility_cache, "w") as f:
            json.dump(cache, f)
    except OSError:
        pass
    return vis == "PUBLIC"


# --- what a tool call would publish ---------------------------------------------------------------

def _read(path: str) -> str:
    try:
        with open(path, errors="replace") as f:
            return f.read(READ_LIMIT)
    except OSError:
        return ""


def _message_texts(args: list[str], here: str) -> list[str]:
    """The message texts of a `git commit` or `git tag`: -m (also bundled, -am, and attached,
    -mtext), --message, --message=, and the contents of -F/--file."""
    out = []
    i = 0
    while i < len(args):
        a = args[i]
        nxt = args[i + 1] if i + 1 < len(args) else None
        if a == "--":
            break
        if a in ("-m", "--message") and nxt is not None:
            out.append(nxt)
            i += 1
        elif a.startswith("--message="):
            out.append(a.split("=", 1)[1])
        elif a in GIT_FILE_FLAGS and nxt is not None:
            out.append(_read(os.path.join(here, expand(nxt))))
            i += 1
        elif a.startswith("--file="):
            out.append(_read(os.path.join(here, expand(a.split("=", 1)[1]))))
        elif a.startswith("-") and not a.startswith("--") and len(a) > 2:
            # bundled short options, as git reads them: -am text, -mtext, -aF file
            for k, letter in enumerate(a[1:], 1):
                if letter in ("m", "F"):
                    if k == len(a) - 1:
                        value, i = nxt, i + 1
                    else:
                        value = a[k + 1:]
                    if value is not None:
                        out.append(value if letter == "m" else _read(os.path.join(here, expand(value))))
                    break
                if letter in "CctSu":           # an option whose value follows: no message here
                    break
        i += 1
    return out


def published(command: str, cwd: str) -> list[tuple[str, str, str]]:
    """(where, target, text) for every message a git or gh command on the line would publish.
    The target is a folder, or `slug:owner/repo` when the command names its repository."""
    found = []
    for inv in git_invocations(command, cwd):
        if inv["sub"] in ("commit", "tag"):
            for text in _message_texts(inv["args"], inv["dir"]):
                found.append((f"the {inv['sub']} message", inv["dir"], text))
    for cmd, here in steps(command, cwd):
        words = cmd["words"]
        if os.path.basename(words[0]) != "gh" or len(words) < 2 or words[1] not in ("pr", "issue", "release"):
            continue
        repo = None
        for i, a in enumerate(words):
            if a in ("-R", "--repo") and i + 1 < len(words):
                repo = "slug:" + words[i + 1]
            elif a.startswith("--repo="):
                repo = "slug:" + a.split("=", 1)[1]
        target = repo or here
        where = f"the gh {words[1]} text"
        for i, a in enumerate(words):
            nxt = words[i + 1] if i + 1 < len(words) else None
            if a in GH_TEXT_FLAGS and nxt is not None:
                found.append((where, target, nxt))
            elif a in GH_FILE_FLAGS and nxt is not None and nxt != "-":
                found.append((where, target, _read(os.path.join(here, expand(nxt)))))
            elif "=" in a and a.split("=", 1)[0] in GH_TEXT_FLAGS:
                found.append((where, target, a.split("=", 1)[1]))
            elif "=" in a and a.split("=", 1)[0] in GH_FILE_FLAGS:
                found.append((where, target, _read(os.path.join(here, expand(a.split("=", 1)[1])))))
    return found


def strings(obj) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in strings(v)]
    return []


def _slug(target: str) -> str | None:
    return target[5:] if target.startswith("slug:") else slug_of(target)


def _hit(texts: list[str], slug: str | None, cfg: Config) -> str | None:
    """The first term TEXTS carry when SLUG is a public repository, else None. Visibility is asked
    only when some term matched, so clean text costs no call to GitHub."""
    widest = cfg.terms_for(personal=False)
    if not any(offending(t, widest, cfg.allow) for t in texts if t):
        return None
    if not is_public(slug, cfg):
        return None
    terms = cfg.terms_for(cfg.is_personal(slug))
    for t in texts:
        hit = offending(t, terms, cfg.allow)
        if hit:
            return hit
    return None


def _commit_hit(command: str, cwd: str, cfg: Config) -> str | None:
    """A term in what a `git commit` on this line would record into a public repository."""
    tops = commit_repos(command, cwd)
    if not tops and hidden_commit(command):
        # the words do not show the commit (a wrapper program, a shell given a string), so what is
        # staged and what -a would take in this folder are read anyway
        command = "git commit -a"
        tops = commit_repos(command, cwd)
    widest = cfg.terms_for(personal=False)
    for top in tops:
        rows, paths = recorded(command, cwd, top)
        # a recorded line, seen through UTF-16 (a NUL beside each letter), and each file name
        items = [(p, n, t.replace("\x00", "")) for p, n, t in rows] + [(p, 0, p) for p in paths]
        if not any(offending(t, widest, cfg.allow) for _, _, t in items):
            continue
        slug = slug_of(top)
        if not is_public(slug, cfg):
            continue
        terms = cfg.terms_for(cfg.is_personal(slug))
        hits = [(p, n, h) for p, n, t in sorted(items) for h in [offending(t, terms, cfg.allow)] if h]
        if hits:
            path, line, hit = hits[0]
            where = f"{path}:{line}" if line else f"the file name {path}"
            more = f" (and {len(hits) - 1} more)" if len(hits) > 1 else ""
            return f"{hit!r} in {where}{more}, which this commit would record"
    return None


def check(payload: dict, cfg: Config) -> str | None:
    """Why this tool call is refused, or None."""
    if not cfg.terms and not cfg.terms_except_personal and not cfg.term_files:
        return None
    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or os.getcwd()
    if tool == "Bash":
        cmd = str(ti.get("command", ""))
        for where, target, text in published(cmd, cwd):
            hit = _hit([text], _slug(target), cfg)
            if hit:
                return f"{hit!r} in {where}"
        for path, text in writes(cmd, cwd):
            hit = _hit([text], slug_of(os.path.dirname(path)), cfg)
            if hit:
                return f"{hit!r} in the text written to {os.path.basename(path)}"
        if any(inv["sub"] == "commit" and commit_plan(inv) is not None
               for inv in git_invocations(cmd, cwd)) or hidden_commit(cmd):
            return _commit_hit(cmd, cwd, cfg)
        return None
    if tool in ("Write", "Edit", "MultiEdit"):
        path = str(ti.get("file_path", ""))
        if tool == "Write":
            texts = [str(ti.get("content", ""))]
        elif tool == "Edit":
            texts = [str(ti.get("new_string", ""))]
        else:
            texts = [str(e.get("new_string", "")) for e in ti.get("edits") or [] if isinstance(e, dict)]
        hit = _hit(texts, slug_of(os.path.dirname(path)), cfg)
        return f"{hit!r} in {os.path.basename(path)}" if hit else None
    if tool.startswith("mcp__github__"):
        owner, repo = ti.get("owner"), ti.get("repo")
        if owner and repo:
            texts = [s for k, v in ti.items() if k not in GITHUB_TARGET_KEYS for s in strings(v)]
            hit = _hit(texts, f"{owner}/{repo}", cfg)
            return f"{hit!r} in the GitHub call" if hit else None
    return None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    path = None
    if argv[:1] == ["--config"] and len(argv) > 1:
        path, argv = argv[1], argv[2:]
    cfg = load(config_path(path))
    if argv == ["--terms"]:
        print("\n".join(cfg.terms_for(personal=False)))
        return 0
    if len(argv) == 2 and argv[0] == "--terms-for":
        print("\n".join(cfg.terms_for(cfg.is_personal(argv[1]))))
        return 0
    if argv == ["--allow"]:
        print("\n".join(cfg.allow))
        return 0
    try:
        payload = json.load(sys.stdin)
        why = check(payload, cfg)
    except Exception:  # noqa: BLE001 - a broken hook must never stop work
        return 0
    if why:
        tail = f" {cfg.message}" if cfg.message else ""
        print(f"Blocked: {why} would go into a public repository, and that term is on the list of "
              f"words kept out of public repositories. Rewrite it for a reader outside the private "
              f"projects and run it again.{tail}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

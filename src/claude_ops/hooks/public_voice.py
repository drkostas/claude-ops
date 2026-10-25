"""Keep private names out of public repositories (a PreToolUse hook).

A person who keeps private projects beside public ones has words that must never reach the public
side: the names of private projects and machines, internal paths and addresses, personal details.
A model writing a commit, a README or a pull request does not always remember which repository is
public, so this hook refuses the tool call instead (exit 2, with the reason sent back to the model).

The words are regular expressions in a JSON file the user keeps outside every repository. The hook
checks text only when its target repository is public on GitHub (asked with `gh repo view`, and
cached), and it checks:

- the content of Write, Edit and MultiEdit calls for a file inside a public repository (not one
  inside `.git` or one git ignores);
- a `git commit`, `tag`, `merge` or `notes` message (`-m`, `--message`, `--trailer`, `-F`/`--file`,
  including `-F -` with a here-document, a file written earlier on the same line, and
  `"$(cat <<EOF ...)"` or `"$(cat FILE)"`), and the text of a `gh pr`, `gh issue`, `gh release` or
  `gh repo` command that writes (title, body, notes, subject, description, and their files), a
  `gh api` call to `repos/OWNER/REPO/...` and its fields, with `-R`, `--repo` or `GH_REPO`;
- what a `git commit` would record: the staged changes, the tracked changes for `-a`, what a
  `git add` on the same line would stage and the paths the commit names, with the file names
  (read through `commandline`, which follows `git -C`, `cd`, line continuations, here-documents,
  comments and wrapper programs such as `timeout`);
- what a `git push` would publish: the lines, file names and messages of the commits no remote has
  yet, however they were made (in a script, by `cherry-pick`, `am` or `stash pop`);
- text a Bash command writes into a file inside a public repository: a here-document given to
  `cat` or `tee` whose output goes to the file, `echo` or `printf` redirected to it or piped into
  `tee`;
- the text arguments of a GitHub MCP call that writes to a public owner/repo;
- and, on a line that publishes to a public repository, the whole command with its path words left
  out, for what the checks above cannot read (a message built from a variable, a shell given a
  string). Text a program makes itself (a script, `sed -i`) is only seen once it is committed or
  pushed.

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
    PATH_WORD, READ_LIMIT, commit_plan, commit_repos, expand, git_invocations, hidden_commit, push_plans,
    push_url, pushed, recorded, steps, unescape, writes,
)

DEFAULT_CONFIG = "~/.config/claude-ops/public-voice.json"
CONFIG_ENV = "CLAUDE_OPS_PUBLIC_VOICE_CONFIG"
DEFAULT_CACHE = "~/.cache/claude-ops/public-voice-visibility.json"
#: how long a repository's visibility is trusted before it is asked again
TTL_S = 6 * 3600
#: `gh` options whose value is published text, and those that name a file holding it
GH_TEXT_FLAGS = {"--title", "-t", "--body", "-b", "--notes", "-n", "--message", "-m", "--subject"}
#: the `gh` subcommands that publish text; others (checkout, list, view) only read or act locally
GH_WRITES = {"pr": {"create", "edit", "comment", "review", "merge", "close", "reopen", "ready"},
             "issue": {"create", "edit", "comment", "close", "reopen", "develop", "transfer"},
             "release": {"create", "edit", "upload"},
             "repo": {"create", "edit"}}
#: `gh repo create|edit` options whose value is shown on the repository's page
GH_REPO_TEXT_FLAGS = {"--description", "-d", "--homepage", "-h"}
#: `$(cat FILE)` in a message: the file's text is the message
CAT_SUBST = re.compile(r"\$\(\s*cat\s+([^\s)]+)\s*\)")
#: `gh api` options whose value is a field sent to GitHub, and the one that names a request body file
GH_API_FIELDS = {"-f", "--raw-field", "-F", "--field"}
#: git commands whose message is published, read the way `git commit` reads it
MESSAGE_SUBCOMMANDS = ("commit", "tag", "merge", "notes")
GH_FILE_FLAGS = {"--body-file", "-F", "--notes-file"}
#: `git commit|tag` options that name a message file
GIT_FILE_FLAGS = {"-F", "--file"}
#: keys of a GitHub MCP call that name its target rather than carry text (a branch name is
#: published, so it is text)
GITHUB_TARGET_KEYS = {"owner", "repo", "path", "sha"}
#: GitHub MCP tools that only read
GITHUB_READS = ("get_", "list_", "search_")


class Config:
    """The lists from the configuration file."""

    def __init__(self, data: dict | None = None):
        data = data if isinstance(data, dict) else {}

        def strs(key: str) -> list[str]:
            v = data.get(key) or []
            if isinstance(v, str):
                v = [v]                    # a single term written without its list
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
    clean = unescape(text)
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

def _read(path: str, written: dict | None = None) -> str:
    """A file's text: what an earlier command on the same line writes into it, else what it holds."""
    path = os.path.normpath(path)
    if written and path in written:
        return written[path]
    try:
        with open(path, errors="replace") as f:
            return f.read(READ_LIMIT)
    except OSError:
        return ""


def _message_texts(args: list[str], here: str, stdin: str | None = None,
                   written: dict | None = None) -> list[str]:
    """The message texts of a `git commit`, `tag`, `merge` or `notes`: -m (also bundled, -am, and
    attached, -mtext), --message, --message=, --trailer, and -F/--file, which is a file (written
    earlier on the line, or on disk) or, as `-`, the here-document given on stdin."""
    def file_text(name: str) -> str:
        if name == "-":
            return stdin or ""
        return _read(os.path.join(here, expand(name)), written)

    out = []
    i = 0
    while i < len(args):
        a = args[i]
        nxt = args[i + 1] if i + 1 < len(args) else None
        if a == "--":
            break
        if a in ("-m", "--message", "--trailer") and nxt is not None:
            out.append(nxt)
            i += 1
        elif a.startswith(("--message=", "--trailer=")):
            out.append(a.split("=", 1)[1])
        elif a in GIT_FILE_FLAGS and nxt is not None:
            out.append(file_text(nxt))
            i += 1
        elif a.startswith("--file="):
            out.append(file_text(a.split("=", 1)[1]))
        elif a.startswith("-") and not a.startswith("--") and len(a) > 2:
            # bundled short options, as git reads them: -am text, -mtext, -aF file
            for k, letter in enumerate(a[1:], 1):
                if letter in ("m", "F"):
                    if k == len(a) - 1:
                        value, i = nxt, i + 1
                    else:
                        value = a[k + 1:]
                    if value is not None:
                        out.append(value if letter == "m" else file_text(value))
                    break
                if letter in "CctSu":           # an option whose value follows: no message here
                    break
        i += 1
    return out


def _with_cat(text: str, here: str, written: dict | None) -> str:
    """A message with each `$(cat FILE)` followed by the file's text, which is what is sent."""
    return text + "".join("\n" + _read(os.path.join(here, expand(m.group(1))), written)
                          for m in CAT_SUBST.finditer(text))


def _gh_api(words: list[str], here: str, written: dict) -> tuple[str | None, list[str]]:
    """The repository a `gh api` call writes to (from its `repos/OWNER/REPO/...` path) and the text
    it sends, or (None, []) for a read or a call with no repository in its path."""
    method, slug, texts = None, None, []
    i = 2
    while i < len(words):
        a = words[i]
        nxt = words[i + 1] if i + 1 < len(words) else None
        if a in ("-X", "--method") and nxt is not None:
            method, i = nxt.upper(), i + 1
        elif a.startswith("--method="):
            method = a.split("=", 1)[1].upper()
        elif a in GH_API_FIELDS and nxt is not None:
            value = nxt.split("=", 1)[1] if "=" in nxt else ""
            texts.append(_read(os.path.join(here, expand(value[1:])), written)
                         if a in ("-F", "--field") and value.startswith("@") else value)
            i += 1
        elif a == "--input" and nxt is not None:
            texts.append(_read(os.path.join(here, expand(nxt)), written))
            i += 1
        elif not a.startswith("-") and slug is None:
            m = re.match(r"/?repos/([^/\s]+)/([^/?#\s]+)", a)
            if m:
                slug = f"{m.group(1)}/{m.group(2)}"
        i += 1
    if slug is None or not texts or method == "GET":
        return None, []
    return slug, texts


def _gh_words(words: list[str]) -> tuple[list[str], str | None]:
    """A gh command's words with its `-R/--repo X` taken out wherever it stands (before, between or
    after the subcommand's words), and X."""
    repo, out, i = None, [], 0
    while i < len(words):
        w = words[i]
        if w in ("-R", "--repo") and i + 1 < len(words):
            repo, i = words[i + 1], i + 2
            continue
        if w.startswith("--repo="):
            repo = w.split("=", 1)[1]
        else:
            out.append(w)
        i += 1
    return out, repo


def _gh_target(words: list[str], here: str, env: dict | None = None, repo: str | None = None) -> str:
    """The repository a gh command names: -R/--repo, GH_REPO in front of it, the OWNER/REPO a
    `gh repo` command takes (as `public:` when it creates a public one), else the folder it runs in."""
    for i, a in enumerate(words):
        if a in ("-R", "--repo") and i + 1 < len(words):
            return "slug:" + words[i + 1]
        if a.startswith("--repo="):
            return "slug:" + a.split("=", 1)[1]
    if words[1:2] == ["repo"]:
        named = next((w for w in words[3:] if not w.startswith("-") and "/" in w), None)
        if named and words[2] == "create" and "--public" in words:
            return "public:" + named
        if named:
            return "slug:" + named
    if repo:
        return "slug:" + repo
    if env and env.get("GH_REPO"):
        return "slug:" + env["GH_REPO"]
    return here


def published(command: str, cwd: str) -> list[tuple[str, str, str]]:
    """(where, target, text) for every message a git or gh command on the line would publish.
    The target is a folder, or `slug:owner/repo` when the command names its repository."""
    written = dict(writes(command, cwd))
    found = []
    for inv in git_invocations(command, cwd):
        if inv["sub"] in MESSAGE_SUBCOMMANDS:
            for text in _message_texts(inv["args"], inv["dir"], inv.get("stdin"), written):
                found.append((f"the {inv['sub']} message", inv["dir"], _with_cat(text, inv["dir"], written)))
    for cmd, here in steps(command, cwd):
        words, repo = _gh_words(cmd["words"])
        if os.path.basename(words[0]) != "gh" or len(words) < 2:
            continue
        if words[1] == "api":
            slug, texts = _gh_api(words, here, written)
            found += [("the gh api call", "slug:" + slug, t) for t in texts] if slug else []
            continue
        if len(words) < 3 or words[2] not in GH_WRITES.get(words[1], ()):
            continue
        target = _gh_target(words, here, cmd.get("env"), repo)
        where = f"the gh {words[1]} text"
        flags = GH_REPO_TEXT_FLAGS if words[1] == "repo" else GH_TEXT_FLAGS
        for i, a in enumerate(words):
            nxt = words[i + 1] if i + 1 < len(words) else None
            if a in flags and nxt is not None:
                found.append((where, target, nxt))
            elif a in GH_FILE_FLAGS and nxt is not None:
                found.append((where, target, (cmd["heredoc"] or "") if nxt == "-"
                               else _read(os.path.join(here, expand(nxt)), written)))
            elif "=" in a and a.split("=", 1)[0] in flags:
                found.append((where, target, a.split("=", 1)[1]))
            elif "=" in a and a.split("=", 1)[0] in GH_FILE_FLAGS:
                name = a.split("=", 1)[1]
                found.append((where, target, (cmd["heredoc"] or "") if name == "-"
                              else _read(os.path.join(here, expand(name)), written)))
    return found


def publish_targets(command: str, cwd: str) -> list[str]:
    """Every repository the line publishes text to (a folder, or `slug:owner/repo`): git commands
    that record a message, gh commands that write, and the current folder when a commit hides
    behind a shell given a string."""
    targets = [inv["dir"] for inv in git_invocations(command, cwd) if inv["sub"] in MESSAGE_SUBCOMMANDS]
    for cmd, here in steps(command, cwd):
        words, repo = _gh_words(cmd["words"])
        if os.path.basename(words[0]) != "gh" or len(words) < 2:
            continue
        if words[1] == "api":
            slug, _ = _gh_api(words, here, {})
            if slug:
                targets.append("slug:" + slug)
        elif len(words) >= 3 and words[2] in GH_WRITES.get(words[1], ()):
            targets.append(_gh_target(words, here, cmd.get("env"), repo))
    if hidden_commit(command):
        targets.append(cwd)
    return list(dict.fromkeys(targets))


def strings(obj) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in strings(v)]
    return []


def _slug(target: str) -> str | None:
    if target.startswith(("slug:", "public:")):
        return target.split(":", 1)[1]
    return slug_of(target)


def not_published(path: str) -> bool:
    """A file that never reaches GitHub from its repository: one inside `.git`, or one git ignores."""
    path = os.path.abspath(path)
    if ".git" in path.split(os.sep):
        return True
    d = os.path.dirname(path)
    while d and not os.path.isdir(d):
        parent = os.path.dirname(d)
        if parent == d:
            return False
        d = parent
    try:
        r = subprocess.run(["git", "-C", d, "check-ignore", "-q", path],
                           capture_output=True, timeout=4)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def _hit(texts: list[str], slug: str | None, cfg: Config, path: str | None = None,
         known_public: bool = False) -> str | None:
    """The first term TEXTS carry when SLUG is a public repository, else None. Visibility is asked
    only when some term matched, so clean text costs no call to GitHub. A PATH that is never
    published (inside .git, or ignored) carries nothing."""
    widest = cfg.terms_for(personal=False)
    if not any(offending(t, widest, cfg.allow) for t in texts if t):
        return None
    if path and not_published(path):
        return None
    if not known_public and not is_public(slug, cfg):
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
        # the words do not show the commit (a shell given a string), so what is staged and what -a
        # would take in this folder are read anyway
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


def _push_hit(command: str, cwd: str, cfg: Config) -> str | None:
    """A term in what a `git push` on this line would publish to a public repository: the lines,
    file names and messages of the commits no remote has yet, however they were made."""
    widest = cfg.terms_for(personal=False)
    for plan in push_plans(command, cwd):
        rows, names, messages = pushed(plan)
        items = ([(p, n, t.replace("\x00", "")) for p, n, t in rows] + [(p, 0, p) for p in names]
                 + [("", -1, m) for m in messages])
        if not any(offending(t, widest, cfg.allow) for _, _, t in items):
            continue
        m = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(\.git)?/?$", push_url(plan))
        slug = f"{m.group(1)}/{m.group(2)}" if m else None
        if not is_public(slug, cfg):
            continue
        terms = cfg.terms_for(cfg.is_personal(slug))
        for path, line, text in items:
            hit = offending(text, terms, cfg.allow)
            if hit:
                where = ("a commit message" if line < 0 else f"{path}:{line}" if line
                         else f"the file name {path}")
                return f"{hit!r} in {where}, which this push would publish"
    return None


def _command_hit(command: str, cwd: str, cfg: Config) -> str | None:
    """A term anywhere in a command line that publishes to a public repository. The checks above
    name where a term is; this one catches what they cannot read (a message built from a
    variable, a shell given a string, a file a command changes before the commit). Path words
    are left out: they say where things go, not what is published."""
    targets = publish_targets(command, cwd)
    if not targets:
        return None
    text = PATH_WORD.sub(" ", command)
    for target in targets:
        hit = _hit([text], _slug(target), cfg, known_public=target.startswith("public:"))
        if hit:
            return (f"{hit!r} in the command, which publishes to a public repository (if the term is "
                    f"only in a step that publishes nothing, run that step in a Bash call of its own)")
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
            hit = _hit([text], _slug(target), cfg, known_public=target.startswith("public:"))
            if hit:
                return f"{hit!r} in {where}"
        for path, text in writes(cmd, cwd):
            hit = _hit([text], slug_of(os.path.dirname(path)), cfg, path)
            if hit:
                return f"{hit!r} in the text written to {os.path.basename(path)}"
        if any(inv["sub"] == "commit" and commit_plan(inv) is not None
               for inv in git_invocations(cmd, cwd)) or hidden_commit(cmd):
            hit = _commit_hit(cmd, cwd, cfg)
            if hit:
                return hit
        hit = _push_hit(cmd, cwd, cfg)
        if hit:
            return hit
        return _command_hit(cmd, cwd, cfg)
    if tool in ("Write", "Edit", "MultiEdit"):
        path = str(ti.get("file_path", ""))
        if tool == "Write":
            texts = [str(ti.get("content", ""))]
        elif tool == "Edit":
            texts = [str(ti.get("new_string", ""))]
        else:
            texts = [str(e.get("new_string", "")) for e in ti.get("edits") or [] if isinstance(e, dict)]
        hit = _hit(texts, slug_of(os.path.dirname(path)), cfg, path)
        return f"{hit!r} in {os.path.basename(path)}" if hit else None
    if tool.startswith("mcp__github__") and not tool[len("mcp__github__"):].startswith(GITHUB_READS):
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

"""missing_names: the names a test file needs that a base revision's modules do not have.

A red proof runs a branch's new test checks against the base branch, to show that they fail there.
When a check reads a name only the branch has (a new function, constant, module or parameter), the
run on the base crashes with AttributeError, ImportError or TypeError instead of failing, and proves
nothing. The crash is only found after the run, yet the files alone show it in seconds. This reads
them and lists every such name before any run:

    python3 -m claude_ops.missing_names --base origin/main tests/test_x.py [more files]
    claude-ops missing-names --base origin/main tests/test_x.py

For each file it finds the modules the file imports from the repository, reads each one as it is at
the base revision (``git show``), and reports, one line each as ``path:line: what``:

- a module the file imports that does not exist at the base;
- a name the file imports from a module, or reads as ``module.name``, that the module does not define
  at the base;
- a call ``module.f(...)`` whose positional arguments or keyword names ``f`` at the base cannot take,
  or that leaves out an argument ``f`` at the base requires.

A use inside a ``try`` whose handler catches the error, or under ``if hasattr(module, "name")``, is
guarded and not reported, and so is ``getattr(module, "name", default)``, which is no attribute read.
A module whose names cannot be known from its text (``from x import *``, a module-level
``__getattr__``) is left out, and so is a decorated function's signature. Writing an attribute
(``module.NAME = value``) never fails, so it is not a use.

It reads text and runs nothing, so a name a module makes only when it runs (through ``globals()``,
``exec`` or ``setattr`` on the module itself) reads as missing although the run would find it. The
summary line says so, so a reader who meets one knows the form at once.

Exit 0 when nothing is missing, 1 when something is, 2 on a usage error (an unknown base, a file
that is not there or cannot be parsed).
"""
from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: what every module has without defining it
MODULE_ATTRS = {"__file__", "__name__", "__doc__", "__dict__", "__spec__", "__loader__", "__package__",
                "__path__", "__builtins__", "__cached__", "__annotations__"}
#: the handlers that catch what a missing name, module or argument raises
CATCHES = {"AttributeError", "ImportError", "ModuleNotFoundError", "TypeError", "NameError",
           "Exception", "BaseException"}


@dataclass(frozen=True)
class Miss:
    path: str
    line: int
    what: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.what}"


class Base:
    """The repository's files at one revision, read through git and kept once read."""

    def __init__(self, repo: Path, ref: str):
        self.repo, self.ref = repo, ref
        self._text: dict[str, str | None] = {}
        self._defs: dict[str, dict | None] = {}

    def text(self, rel: str) -> str | None:
        if rel not in self._text:
            r = subprocess.run(["git", "-C", str(self.repo), "show", f"{self.ref}:{rel}"],
                               capture_output=True, text=True)
            self._text[rel] = r.stdout if r.returncode == 0 else None
        return self._text[rel]

    def defs(self, rel: str) -> dict | None:
        """{name: node or None} a module defines at the top level at this revision, or None when its
        names cannot be known from its text (a star import, a module-level __getattr__, a syntax error)."""
        if rel not in self._defs:
            text = self.text(rel)
            self._defs[rel] = None if text is None else _top_level(text)
        return self._defs[rel]


def _targets(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, (ast.Tuple, ast.List)):
        return [n for e in node.elts for n in _targets(e)]
    if isinstance(node, ast.Starred):
        return _targets(node.value)
    return []


def _top_level(text: str) -> dict | None:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    names: dict = {}

    def walk(body):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names[node.name] = node
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    for n in _targets(t):
                        names[n] = None
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                for n in _targets(node.target):
                    names[n] = None
            elif isinstance(node, ast.Import):
                for a in node.names:
                    names[a.asname or a.name.split(".")[0]] = None
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    if a.name == "*":
                        return False
                    names[a.asname or a.name] = None
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                for n in _targets(node.target):
                    names[n] = None
                if walk(node.body) is False or walk(node.orelse) is False:
                    return False
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None:
                        for n in _targets(item.optional_vars):
                            names[n] = None
                if walk(node.body) is False:
                    return False
            elif isinstance(node, ast.If):
                if walk(node.body) is False or walk(node.orelse) is False:
                    return False
            elif isinstance(node, ast.Try):
                for part in (node.body, node.orelse, node.finalbody, *[h.body for h in node.handlers]):
                    if walk(part) is False:
                        return False
        return True

    if walk(tree.body) is False or isinstance(names.get("__getattr__"), ast.FunctionDef):
        return None
    return names


class Finder:
    """Where a dotted module name lives in the repository: its file relative to the repository, or None
    for a module from outside it (the standard library, an installed package)."""

    def __init__(self, repo: Path, roots: list[Path]):
        self.repo, self.roots = repo, roots

    def file(self, dotted: str) -> str | None:
        parts = dotted.split(".")
        for root in self.roots:
            for cand in (root.joinpath(*parts).with_suffix(".py"), root.joinpath(*parts, "__init__.py")):
                if cand.is_file():
                    try:
                        return str(cand.resolve().relative_to(self.repo))
                    except ValueError:
                        return None
        return None


def _guarded_by(node: ast.AST) -> set:
    """What an `if` test or an IfExp test guards: {(name, attribute)} from hasattr(name, "attr") calls."""
    out = set()
    for n in ast.walk(node):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "hasattr"
                and len(n.args) == 2 and isinstance(n.args[0], ast.Name)
                and isinstance(n.args[1], ast.Constant) and isinstance(n.args[1].value, str)):
            out.add((n.args[0].id, n.args[1].value))
    return out


def _catches(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    names = [handler.type] if not isinstance(handler.type, ast.Tuple) else list(handler.type.elts)
    return any(isinstance(n, ast.Name) and n.id in CATCHES or isinstance(n, ast.Attribute) and n.attr in CATCHES
               for n in names)


def _chain(node: ast.Attribute) -> tuple[str, list[str]] | None:
    """(the name at the root, the attributes read after it) of a plain a.b.c chain, else None."""
    attrs = []
    while isinstance(node, ast.Attribute):
        attrs.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    return node.id, attrs[::-1]


def _signature_miss(fn: ast.FunctionDef, call: ast.Call) -> str | None:
    """Why `call` cannot call `fn` as defined, or None."""
    if fn.decorator_list or any(isinstance(a, ast.Starred) for a in call.args) \
            or any(k.arg is None for k in call.keywords):
        return None
    a = fn.args
    positional = [p.arg for p in (*getattr(a, "posonlyargs", []), *a.args)]
    if positional and positional[0] in ("self", "cls"):
        return None
    given = len(call.args)
    if a.vararg is None and given > len(positional):
        return f"takes at most {len(positional)} positional argument(s), the test passes {given}"
    named = {k.arg for k in call.keywords}
    allowed = set(positional[len(getattr(a, "posonlyargs", [])):]) | {p.arg for p in a.kwonlyargs}
    if a.kwarg is None:
        extra = sorted(named - allowed)
        if extra:
            return f"has no parameter {', '.join(extra)}"
    required = positional[:len(positional) - len(a.defaults)]
    missing = [p for i, p in enumerate(required) if i >= given and p not in named]
    missing += [p.arg for p, d in zip(a.kwonlyargs, a.kw_defaults) if d is None and p.arg not in named]
    if missing:
        return f"requires {', '.join(missing)}, which the test does not pass"
    return None


def check_file(path: Path, base: Base, finder: Finder, shown: str | None = None) -> list[Miss]:
    """Every name, module or argument `path` needs that the base revision lacks, unguarded."""
    shown = shown or str(path)
    tree = ast.parse(path.read_text(), filename=str(path))
    misses: list[Miss] = []
    bound: dict[str, str] = {}      # a name in the test -> the module it is bound to
    seen = set()
    # A name the file also assigns (a variable, a parameter, a loop target) may not be the module where it is
    # read, and an attribute the file sets on a module (module.NAME = ..., setattr(module, "NAME", ...)) exists
    # by the time it is read. Neither is reported.
    stored = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))}
    stored |= {a.arg for a in ast.walk(tree) if isinstance(a, ast.arg)}
    stored |= {h.name for h in ast.walk(tree) if isinstance(h, ast.ExceptHandler) and h.name}
    set_attrs = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store):
            got = _chain(n)
            if got:
                set_attrs.add((got[0], got[1][0]))
        elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "setattr"
              and len(n.args) >= 2 and isinstance(n.args[0], ast.Name)
              and isinstance(n.args[1], ast.Constant) and isinstance(n.args[1].value, str)):
            set_attrs.add((n.args[0].id, n.args[1].value))

    def reported(name: str, attr: str, hasattrs: frozenset) -> bool:
        return name in bound and name not in stored and (name, attr) not in set_attrs and (name, attr) not in hasattrs

    def miss(line: int, what: str) -> None:
        if (line, what) not in seen:
            seen.add((line, what))
            misses.append(Miss(shown, line, what))

    def module_file(dotted: str) -> tuple[str | None, bool]:
        """(the module's file in the repository, whether it exists at the base)."""
        rel = finder.file(dotted)
        return rel, rel is not None and base.text(rel) is not None

    def has(dotted: str, name: str) -> bool | None:
        """Whether module `dotted` defines `name` at the base: True, False, or None when that cannot
        be known (outside the repository, or a module whose names cannot be read)."""
        if name in MODULE_ATTRS or module_file(f"{dotted}.{name}")[1]:
            return True                 # a submodule, which a package without __init__.py holds too
        rel, there = module_file(dotted)
        if rel is None or not there:
            return None
        defs = base.defs(rel)
        return None if defs is None else name in defs

    def visit(node: ast.AST, guarded: bool, hasattrs: frozenset) -> None:
        if isinstance(node, ast.Try):
            inner = guarded or any(_catches(h) for h in node.handlers)
            for n in node.body:
                visit(n, inner, hasattrs)
            for part in (node.orelse, node.finalbody, *[h.body for h in node.handlers]):
                for n in part:
                    visit(n, guarded, hasattrs)
            return
        if isinstance(node, (ast.If, ast.IfExp, ast.While)):
            more = frozenset(hasattrs | _guarded_by(node.test))
            visit(node.test, guarded, hasattrs)
            body = node.body if isinstance(node.body, list) else [node.body]
            for n in body:
                visit(n, guarded, more)
            orelse = node.orelse if isinstance(node.orelse, list) else [node.orelse]
            for n in orelse:
                visit(n, guarded, hasattrs)
            return
        if isinstance(node, ast.Import):
            for a in node.names:
                rel, there = module_file(a.name)
                if rel is not None and not there and not guarded:
                    miss(node.lineno, f"imports {a.name}, which does not exist at {base.ref}")
                if a.asname:
                    bound[a.asname] = a.name
                else:
                    top = a.name.split(".")[0]
                    bound.setdefault(top, top)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            rel, there = module_file(node.module)
            if rel is not None and not there:
                if not guarded:
                    miss(node.lineno, f"imports from {node.module}, which does not exist at {base.ref}")
            else:
                for a in node.names:
                    if a.name == "*":
                        continue
                    sub = f"{node.module}.{a.name}"
                    if finder.file(sub) is not None:
                        bound[a.asname or a.name] = sub
                        if not module_file(sub)[1] and not guarded:
                            miss(node.lineno, f"imports {sub}, which does not exist at {base.ref}")
                    elif has(node.module, a.name) is False and not guarded:
                        miss(node.lineno, f"imports {a.name} from {node.module}, which {base.ref} does not define")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and not guarded:
            got = _chain(node.func)
            if got and len(got[1]) == 1 and reported(got[0], got[1][0], hasattrs):
                rel, there = module_file(bound[got[0]])
                defs = base.defs(rel) if rel and there else None
                fn = defs.get(got[1][0]) if defs else None
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    why = _signature_miss(fn, node)
                    if why:
                        miss(node.lineno, f"calls {bound[got[0]]}.{got[1][0]}, which at {base.ref} {why}")
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            got = _chain(node)
            if got is not None:      # a plain a.b.c chain: its inner nodes are the same read
                if not guarded and reported(got[0], got[1][0], hasattrs):
                    dotted = bound[got[0]]
                    for attr in got[1]:
                        known = has(dotted, attr)
                        if known is False:
                            miss(node.lineno, f"reads {dotted}.{attr}, which {base.ref} does not define")
                            break
                        if known is None or not module_file(f"{dotted}.{attr}")[0]:
                            break
                        dotted = f"{dotted}.{attr}"
                return
        for child in ast.iter_child_nodes(node):
            visit(child, guarded, hasattrs)

    for top in tree.body:
        visit(top, False, frozenset())
    return sorted(misses, key=lambda m: (m.line, m.what))


def check(repo: Path, ref: str, files: list[Path], paths: list[Path] = ()) -> list[Miss]:
    """The misses of every file, against `ref` in `repo`. Raises ValueError for an unknown ref."""
    repo = repo.resolve()
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", f"{ref}^{{commit}}"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise ValueError(f"{ref} is not a revision of {repo}")
    base = Base(repo, ref)
    out: list[Miss] = []
    for f in files:
        f = f.resolve()
        roots = [repo, *(repo / p for p in paths), f.parent]
        if (repo / "src").is_dir():
            roots.insert(1, repo / "src")
        try:
            shown = str(f.relative_to(repo))
        except ValueError:
            shown = str(f)
        out += check_file(f, base, Finder(repo, roots), shown)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="missing_names", description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", required=True, help="the revision the files will run against, e.g. origin/main")
    ap.add_argument("--repo", default=None, help="the repository (default: the one holding the current folder)")
    ap.add_argument("--path", action="append", default=[],
                    help="another folder, relative to the repository, that the files import from (repeatable)")
    ap.add_argument("files", nargs="+")
    a = ap.parse_args(argv)
    if a.repo:
        repo = Path(a.repo)
    else:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
        if r.returncode != 0:
            print("missing_names: not inside a git repository; pass --repo", file=sys.stderr)
            return 2
        repo = Path(r.stdout.strip())
    files = [Path(f) for f in a.files]
    for f in files:
        if not f.is_file():
            print(f"missing_names: {f} is not a file", file=sys.stderr)
            return 2
    try:
        found = check(repo, a.base, files, [Path(p) for p in a.path])
    except ValueError as e:
        print(f"missing_names: {e}", file=sys.stderr)
        return 2
    except SyntaxError as e:
        print(f"missing_names: {e.filename} does not parse at line {e.lineno}: {e.msg}", file=sys.stderr)
        return 2
    for m in found:
        print(m)
    if found:
        print(f"{len(found)} name(s) the files need that {a.base} does not have; a run against {a.base} "
              f"would stop at the first one. A name a module makes only when it runs (through globals(), exec or "
              f"setattr on the module itself) reads as missing here, so check such a line in its module", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())

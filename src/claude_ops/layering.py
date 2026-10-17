"""layering.py — find the modules of a private project that hold no private knowledge.

A private project that is built over public packages keeps its own records, rules and wiring, and
the general mechanisms belong in the packages under it, where every user can use them. Over time,
general code gets written in the private project anyway, because that is where the work happened.
This finds the candidates mechanically.

A module is a candidate when nothing it imports, followed all the way down through the project's
own modules, reaches a marker: an import that means "this uses the private side", such as the
database driver the project's record runs on, or the module that writes that record. Such a module
works without the private side, so it can usually move to a package.

It is a list of candidates, not a verdict. A module can reach no marker and still be the project's
own (its migration tooling, its constants). The usual way to use it is a test in the private project
that keeps a reviewed list: each known module with the reason it stays, and every new candidate a
failure until it moves or is reviewed.

    claude-ops layering /path/to/project --package app --marker psycopg --marker app.core.record
    claude-ops layering /path/to/project --package app --marker psycopg --skip app.tests --json

Only `import x` and `from x import y` statements are read. An import made through importlib, or
inside a string, is not seen, and a module that cannot be parsed is reported as unreadable.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path


def module_name(path: Path, root: Path) -> str:
    """`pkg/sub/mod.py` under `root` as `pkg.sub.mod`, and `pkg/sub/__init__.py` as `pkg.sub`."""
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def imports_of(source: str, module: str = "") -> set[str]:
    """Every module name `source` imports. `from a import b` gives both `a` and `a.b`, because b may
    be a module of its own. A relative import is resolved against `module`."""
    out: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = module.split(".")[:-node.level] if module else []
                base = ".".join(anchor + ([base] if base else []))
            if base:
                out.add(base)
                out |= {f"{base}.{a.name}" for a in node.names if a.name != "*"}
    return out


def graph(root: Path, package: str) -> tuple[dict[str, set[str]], dict[str, Path], list[str]]:
    """(imports by module, path by module, unreadable modules) for every .py file of `package`
    under `root`."""
    root = Path(root)
    edges: dict[str, set[str]] = {}
    paths: dict[str, Path] = {}
    unreadable: list[str] = []
    for path in sorted((root / package.replace(".", "/")).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        name = module_name(path, root)
        paths[name] = path
        try:
            edges[name] = imports_of(path.read_text(errors="replace"), name)
        except SyntaxError:
            edges[name] = set()
            unreadable.append(name)
    return edges, paths, unreadable


def _is(name: str, marker: str) -> bool:
    return name == marker or name.startswith(marker + ".")


def reaches(edges: dict[str, set[str]], module: str, markers: list[str]) -> list[str] | None:
    """The import chain from `module` to the first marker it reaches, or None when it reaches none.
    Only the project's own modules (the keys of `edges`) are followed."""
    seen = {module}
    stack = [(module, [module])]
    while stack:
        here, chain = stack.pop()
        for name in sorted(edges.get(here, ())):
            if any(_is(name, m) for m in markers):
                return chain + [name]
            if name in edges and name not in seen:
                seen.add(name)
                stack.append((name, chain + [name]))
    return None


def candidates(root: Path, package: str, markers: list[str], skip: list[str] = ()) -> dict:
    """{"free": [module, ...], "unreadable": [...]}: the modules of `package` that reach no marker,
    leaving out any module under one of the `skip` prefixes (tests, fixtures)."""
    edges, _paths, unreadable = graph(root, package)
    free = [m for m in sorted(edges)
            if not any(_is(m, s) for s in skip) and m not in unreadable
            and reaches(edges, m, list(markers)) is None]
    return {"free": free, "unreadable": unreadable}


def main(root: str, package: str, markers: list[str], skip: list[str], as_json: bool) -> int:
    if not markers:
        print("give at least one --marker: an import that means a module uses the private side")
        return 2
    out = candidates(Path(root), package, markers, skip)
    if as_json:
        print(json.dumps(out, indent=1))
    else:
        for m in out["free"]:
            print(m)
        for m in out["unreadable"]:
            print(f"unreadable: {m}")
    return 0

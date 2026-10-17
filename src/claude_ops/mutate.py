"""mutate — prove a test can fail: change the code on purpose and check that the tests catch it.

    claude-ops mutate <worktree> --run "pytest -q {copy}/tests" \\
        --function src/pkg/vault.py:add_secret [--function ...] [--mutations list.json] \\
        [--caught 1] [--env NAME=VALUE] [--only id ...] [--list] [--out results.jsonl]

A test that passes whatever the code does proves nothing. This writes each mutation into a COPY of
the worktree, runs the command there, and expects it to fail.

THE MUTATIONS. `--function <file>:<name>` makes them from the function's syntax: every `if`,
`while` and conditional expression's test negated, every comparison's operator reversed and its
boundary moved by one (`>=` to `>`, which a test that never tries the edge lets through), `and` and
`or` swapped, `True` and `False` flipped, every `raise` and every call made as a statement replaced
by `pass`, and every returned value replaced by None. Each one replaces the exact span of source it
names, so the rest of the file keeps its comments and layout. `--mutations <file.json>` adds
hand-written ones, a list of {"id", "file", "old", "new", "label"} where `old` must appear exactly
once in the file. `--list` prints them without running anything.

THE RUN. The worktree is copied (no .git, node_modules or caches) to a scratch folder and only the
copy is changed. First the command runs on the unchanged copy: if it does not pass, everything
stops, since a failure that was already there would read as a mutation caught. Then each mutation
is written into the copy, the command runs (`{copy}` in it is the copy's path), and the file is put
back. An exit code listed in `--caught` (default 1) is a mutation caught, 0 is a mutation that
passed, and any other code is a problem with the run itself and stops it. The copy is compared with
the worktree at the end.

It prints each result as it goes and the mutations that passed at the end, and writes one JSON line
per mutation to --out. Exit 0 when every mutation was caught, 1 when any passed, 2 when it could not
run.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

FLIP_OP = {ast.Eq: "!=", ast.NotEq: "==", ast.Lt: ">=", ast.LtE: ">", ast.Gt: "<=", ast.GtE: "<",
           ast.In: "not in", ast.NotIn: "in", ast.Is: "is not", ast.IsNot: "is"}
#: the boundary moved by one: a test that never tries the edge itself lets these through
EDGE_OP = {ast.Lt: "<=", ast.LtE: "<", ast.Gt: ">=", ast.GtE: ">"}
EXCLUDE = (".git", "node_modules", "__pycache__", ".pytest_cache", ".venv")
#: rsync and diff need nothing from the caller's environment
TOOLS_ENV = {"PATH": "/usr/bin:/bin"}


def _offsets(src: bytes) -> list[int]:
    """The byte offset where each line starts (ast columns are byte offsets into a line)."""
    starts, pos = [], 0
    for line in src.splitlines(keepends=True):
        starts.append(pos)
        pos += len(line)
    starts.append(pos)
    return starts


def _span(starts: list[int], node: ast.AST) -> tuple[int, int]:
    return (starts[node.lineno - 1] + node.col_offset,
            starts[node.end_lineno - 1] + node.end_col_offset)


def function_mutations(root: Path, rel: str, name: str) -> list[dict]:
    """The mutations of one function, each a {"id", "file", "start", "end", "new", "label"} that
    replaces the bytes start..end of the file with `new`."""
    src = (root / rel).read_bytes()
    tree = ast.parse(src)
    funcs = [n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    if len(funcs) != 1:
        raise ValueError(f"{rel} defines {name} {len(funcs)} times, not once")
    starts = _offsets(src)
    out: list[dict] = []

    def add(kind: str, node: ast.AST, new: str, label: str) -> None:
        a, b = _span(starts, node)
        out.append({"id": f"{name}:{node.lineno}:{kind}:{len(out)}", "file": rel, "start": a, "end": b,
                    "new": new, "label": f"{name} line {node.lineno}: {label}"})

    def seg(node: ast.AST) -> str:
        a, b = _span(starts, node)
        return src[a:b].decode("utf-8")

    for node in ast.walk(funcs[0]):
        if isinstance(node, (ast.If, ast.While, ast.IfExp)):
            add("not", node.test, f"not ({seg(node.test)})", f"the test `{seg(node.test)[:60]}` negated")
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in FLIP_OP:
            op = FLIP_OP[type(node.ops[0])]
            add("op", node, f"{seg(node.left)} {op} {seg(node.comparators[0])}",
                f"`{seg(node)[:60]}` with {op}")
            if type(node.ops[0]) in EDGE_OP:
                edge = EDGE_OP[type(node.ops[0])]
                add("edge", node, f"{seg(node.left)} {edge} {seg(node.comparators[0])}",
                    f"`{seg(node)[:60]}` with {edge} (the boundary)")
        if isinstance(node, ast.BoolOp):
            other = " or " if isinstance(node.op, ast.And) else " and "
            add("boolop", node, other.join(f"({seg(v)})" for v in node.values),
                f"`{seg(node)[:60]}` with{other.strip()}")
        if isinstance(node, ast.Constant) and isinstance(node.value, bool):
            add("bool", node, str(not node.value), f"{node.value} flipped")
        if isinstance(node, ast.Raise):
            add("noraise", node, "pass", f"`{seg(node)[:60]}` removed")
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            add("nocall", node, "pass", f"the call `{seg(node)[:60]}` removed")
        if isinstance(node, ast.Return) and node.value is not None:
            add("none", node.value, "None", f"`return {seg(node.value)[:50]}` returns None")
    return out


def explicit_mutations(root: Path, path: Path) -> list[dict]:
    """Hand-written mutations from a JSON list; each `old` must appear exactly once in its file."""
    out = []
    for m in json.loads(path.read_text()):
        src = (root / m["file"]).read_bytes()
        old = m["old"].encode("utf-8")
        if src.count(old) != 1:
            raise ValueError(f"{m['id']}: {m['file']} has its old text {src.count(old)} times, not once")
        a = src.index(old)
        out.append({"id": m["id"], "file": m["file"], "start": a, "end": a + len(old),
                    "new": m["new"], "label": m.get("label", m["id"])})
    return out


def copy_tree(root: Path, into: Path) -> None:
    subprocess.run(["rsync", "-a", "--delete", *[f"--exclude={x}" for x in EXCLUDE],
                    f"{root}/", f"{into}/"], check=True, env=TOOLS_ENV)


def drop_bytecode(target: Path) -> None:
    """Remove the file's cached bytecode in the copy. Python trusts a .pyc whose source has the same
    mtime (to the second) and the same size, so a mutation written in the same second as the last
    one, and the same size as it (`>=` to `>` after `>=` to `<`), would run the previous version's
    bytecode: a result that belonged to another mutation."""
    for pyc in (target.parent / "__pycache__").glob(f"{target.stem}.*.pyc"):
        pyc.unlink(missing_ok=True)


def run_command(run: list[str], copy: Path, env: dict | None) -> tuple[int, str]:
    """Run the command with {copy} replaced by the copy's path. No bytecode is written during a
    run, so nothing stale is left for the next one."""
    args = [a.replace("{copy}", str(copy)) for a in run]
    child = dict(os.environ if env is None else env, PYTHONDONTWRITEBYTECODE="1")
    try:
        r = subprocess.run(args, capture_output=True, text=True, env=child, timeout=3600)
        return r.returncode, r.stdout + r.stderr
    except subprocess.TimeoutExpired:
        return -1, "timed out after an hour"
    except OSError as e:
        return -1, f"could not start {args[0]}: {e}"


def run_mutations(root: Path, muts: list[dict], run: list[str], *, caught: tuple = (1,),
                  env: dict | None = None, out: Path | None = None) -> int:
    """Copy `root`, check the command passes on the unchanged copy, then write each mutation into the
    copy, run the command, and put the file back. Exit 0 when every mutation was caught, 1 when any
    passed, 2 when it could not run. `env` is the command's environment (default: this process's)."""
    copy = Path(tempfile.mkdtemp(prefix="mutate-")) / "wt"
    out = out or copy.parent / "results.jsonl"
    copy_tree(root, copy)
    code, said = run_command(run, copy, env)
    if code != 0:
        print(f"REFUSED: the unchanged copy does not pass (exit {code}), so a failure would prove "
              f"nothing:\n{said[-1500:]}", file=sys.stderr)
        return 2
    print(f"baseline passes; {len(muts)} mutation(s), results in {out}")
    survivors = []
    for m in muts:
        target = copy / m["file"]
        pristine = target.read_bytes()
        target.write_bytes(pristine[:m["start"]] + m["new"].encode("utf-8") + pristine[m["end"]:])
        drop_bytecode(target)
        t0 = time.time()
        try:
            code, said = run_command(run, copy, env)
        finally:
            target.write_bytes(pristine)
            drop_bytecode(target)
        if code != 0 and code not in caught:
            print(f"STOPPED at {m['id']}: the run itself failed (exit {code}):\n{said[-800:]}",
                  file=sys.stderr)
            return 2
        hit = code in caught
        rec = {**{k: m[k] for k in ("id", "file", "label")}, "caught": hit, "exit": code,
               "secs": round(time.time() - t0),
               "lines": [ln.strip() for ln in said.splitlines() if "FAIL" in ln or "exit " in ln][:6]}
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        print(f"{'caught' if hit else 'PASSED'} {m['id']}  {m['label']}  ({rec['secs']}s)", flush=True)
        if not hit:
            survivors.append(m)
    diff = subprocess.run(["diff", "-rq", *[f"-x{x}" for x in EXCLUDE], str(root), str(copy)],
                          capture_output=True, text=True, env=TOOLS_ENV)
    print("the copy matches the worktree again" if not diff.stdout.strip()
          else f"THE COPY DIFFERS from the worktree:\n{diff.stdout[:500]}")
    print(f"\n{len(muts) - len(survivors)} of {len(muts)} mutation(s) were caught")
    for m in survivors:
        print(f"  passed anyway: {m['id']}  {m['label']}")
    shutil.rmtree(copy, ignore_errors=True)
    return 1 if survivors else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="claude-ops mutate", description=__doc__.split("\n")[0])
    ap.add_argument("worktree", type=Path)
    ap.add_argument("--run", help="the command that runs the tests, {copy} for the copy's path")
    ap.add_argument("--caught", default="1", help="exit codes that mean a test failed (default 1)")
    ap.add_argument("--env", action="append", default=[], metavar="NAME=VALUE",
                    help="a variable added to the command's environment (repeatable)")
    ap.add_argument("--function", action="append", default=[], metavar="FILE:NAME")
    ap.add_argument("--mutations", type=Path)
    ap.add_argument("--only", nargs="+", default=[])
    ap.add_argument("--list", action="store_true", help="print the mutations and run nothing")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    root = a.worktree.resolve()
    try:
        muts: list[dict] = []
        for spec in a.function:
            rel, _, name = spec.rpartition(":")
            muts += function_mutations(root, rel, name)
        if a.mutations:
            muts += explicit_mutations(root, a.mutations)
        caught = tuple(int(c) for c in a.caught.split(",") if c.strip())
        extra = dict(e.split("=", 1) for e in a.env)
    except (ValueError, OSError, SyntaxError) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    if a.only:
        muts = [m for m in muts if m["id"] in a.only]
    if not muts:
        print("REFUSED: no mutations (name a --function or a --mutations file)", file=sys.stderr)
        return 2
    if a.list:
        for m in muts:
            print(f"{m['id']}\t{m['label']}")
        return 0
    if not a.run or "{copy}" not in a.run:
        print("REFUSED: name the command that runs the tests with --run, with {copy} in it",
              file=sys.stderr)
        return 2
    if 0 in caught:
        print("REFUSED: exit 0 is a pass, never a caught mutation", file=sys.stderr)
        return 2
    return run_mutations(root, muts, shlex.split(a.run), caught=caught,
                         env=dict(os.environ, **extra), out=a.out)


if __name__ == "__main__":
    raise SystemExit(main())

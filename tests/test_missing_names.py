"""missing_names: the names a test file needs that a base revision's modules do not have."""
import subprocess

from claude_ops import missing_names


def git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@example.org", "-c", "user.name=t", *args],
                   check=True, capture_output=True)


def write(root, files):
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def repo(tmp_path, base, branch):
    """A repository whose first commit holds `base`, with `branch` written over it in the working tree."""
    git(tmp_path, "init", "-q")
    write(tmp_path, base)
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    git(tmp_path, "tag", "base")
    write(tmp_path, branch)
    return tmp_path


BASE = {
    "pkg/__init__.py": "",
    "pkg/mod.py": "X = 1\n\ndef f(a, b=0):\n    return a\n",
    "pkg/star.py": "from os.path import *\n",
}
BRANCH = {
    "pkg/mod.py": "X = 1\nY = 2\n\ndef f(a, b=0, c=0):\n    return a\n\ndef g():\n    return 1\n",
    "pkg/new.py": "Z = 3\n",
}


def misses(tmp_path, test_text):
    write(tmp_path, {"tests/test_it.py": test_text})
    return [(m.line, m.what) for m in missing_names.check(tmp_path, "base", [tmp_path / "tests/test_it.py"])]


def test_a_name_only_the_branch_has_is_named_with_its_line(tmp_path):
    root = repo(tmp_path, BASE, BRANCH)
    got = misses(root, "from pkg import mod\nmod.X\nmod.Y\nmod.g()\n")
    assert got == [(3, "reads pkg.mod.Y, which base does not define"),
                   (4, "reads pkg.mod.g, which base does not define")]


def test_a_call_the_base_signature_cannot_take(tmp_path):
    root = repo(tmp_path, BASE, BRANCH)
    got = misses(root, "from pkg import mod\nmod.f(1, c=2)\nmod.f(1, 2, 3)\nmod.f()\nmod.f(1, b=2)\n")
    assert got == [(2, "calls pkg.mod.f, which at base has no parameter c"),
                   (3, "calls pkg.mod.f, which at base takes at most 2 positional argument(s), the test passes 3"),
                   (4, "calls pkg.mod.f, which at base requires a, which the test does not pass")]


def test_imports_of_a_missing_module_or_name(tmp_path):
    root = repo(tmp_path, BASE, BRANCH)
    got = misses(root, "import pkg.new\nfrom pkg.mod import Y, X\nfrom pkg import new as n\n")
    assert got == [(1, "imports pkg.new, which does not exist at base"),
                   (2, "imports Y from pkg.mod, which base does not define"),
                   (3, "imports pkg.new, which does not exist at base")]


def test_guarded_uses_are_not_reported(tmp_path):
    root = repo(tmp_path, BASE, BRANCH)
    got = misses(root, (
        "from pkg import mod\n"
        "g = getattr(mod, 'g', None)\n"
        "try:\n    mod.g()\nexcept AttributeError:\n    pass\n"
        "if hasattr(mod, 'Y'):\n    print(mod.Y)\n"
        "v = mod.Y if hasattr(mod, 'Y') else None\n"
        "try:\n    import pkg.new\nexcept ImportError:\n    pass\n"))
    assert got == []


def test_what_the_file_sets_or_rebinds_and_what_lies_outside_is_not_reported(tmp_path):
    root = repo(tmp_path, BASE, BRANCH)
    got = misses(root, (
        "import json\nfrom pkg import mod, star\nfrom pkg import mod as m2\n"
        "json.not_a_name\n"                       # outside the repository
        "star.anything\n"                         # a star import: its names cannot be known
        "mod.SAVED = mod.X\nprint(mod.SAVED)\n"   # set by the file before it is read
        "setattr(mod, 'LATER', 1)\nmod.LATER\n"
        "m2 = {'items': 1}\nm2.items()\n"         # the alias is rebound to a dict
        "mod.__file__\n"))                        # every module has it
    assert got == []


def test_the_command_line_exits_1_with_lines_and_2_on_an_unknown_base(tmp_path, capsys, monkeypatch):
    root = repo(tmp_path, BASE, BRANCH)
    write(root, {"tests/test_it.py": "from pkg import mod\nmod.Y\n"})
    rc = missing_names.main(["--base", "base", "--repo", str(root), str(root / "tests/test_it.py")])
    out = capsys.readouterr()
    assert rc == 1 and "tests/test_it.py:2: reads pkg.mod.Y, which base does not define" in out.out
    assert "1 name(s) the files need that base does not have" in out.err and "globals(), exec or setattr" in out.err
    write(root, {"tests/test_it.py": "from pkg import mod\nmod.X\n"})
    assert missing_names.main(["--base", "base", "--repo", str(root), str(root / "tests/test_it.py")]) == 0
    assert missing_names.main(["--base", "no-such-ref", "--repo", str(root), str(root / "tests/test_it.py")]) == 2
    assert missing_names.main(["--base", "base", "--repo", str(root), str(root / "tests/missing.py")]) == 2


def test_a_name_made_only_at_run_time_reads_as_missing_and_the_summary_names_that_form(tmp_path, capsys):
    root = repo(tmp_path, {**BASE, "pkg/made.py": "globals()['LATE'] = 1\n"}, {})
    write(root, {"tests/test_it.py": "from pkg import made\nmade.LATE\n"})
    rc = missing_names.main(["--base", "base", "--repo", str(root), str(root / "tests/test_it.py")])
    out = capsys.readouterr()
    assert rc == 1 and "tests/test_it.py:2: reads pkg.made.LATE" in out.out
    assert "makes only when it runs" in out.err

"""layering: the modules of a project that reach no private marker, followed through its own imports."""
from claude_ops import layering


def tree(tmp_path, files):
    for name, text in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path


def test_a_module_that_reaches_a_marker_through_another_is_not_free(tmp_path):
    root = tree(tmp_path, {
        "app/__init__.py": "",
        "app/record.py": "import psycopg\n",
        "app/pages.py": "from app import record\n",
        "app/deep.py": "from app.pages import thing\n",
        "app/plain.py": "import json\nfrom app import helpers\n",
        "app/helpers.py": "import re\n",
    })
    out = layering.candidates(root, "app", ["psycopg"])
    assert out["free"] == ["app", "app.helpers", "app.plain"]


def test_the_chain_names_how_a_module_reaches_the_marker(tmp_path):
    root = tree(tmp_path, {"app/a.py": "from app import b\n", "app/b.py": "from app.c import x\n",
                           "app/c.py": "from app.store import save\n"})
    edges, _, _ = layering.graph(root, "app")
    assert layering.reaches(edges, "app.a", ["app.store"]) == ["app.a", "app.b", "app.c", "app.store"]
    assert layering.reaches(edges, "app.c", ["psycopg"]) is None


def test_relative_imports_and_cycles(tmp_path):
    root = tree(tmp_path, {
        "app/__init__.py": "",
        "app/sub/__init__.py": "",
        "app/sub/one.py": "from . import two\n",
        "app/sub/two.py": "from .one import f\nfrom ..db import conn\n",
        "app/db.py": "import sqlite3\n",
    })
    out = layering.candidates(root, "app", ["sqlite3"])
    assert "app.sub.one" not in out["free"] and "app.sub.two" not in out["free"]


def test_skip_and_unreadable(tmp_path):
    root = tree(tmp_path, {"app/tests/t.py": "import json\n", "app/bad.py": "def (:\n", "app/ok.py": ""})
    out = layering.candidates(root, "app", ["psycopg"], skip=["app.tests"])
    assert out == {"free": ["app.ok"], "unreadable": ["app.bad"]}


def test_main_needs_a_marker(tmp_path, capsys):
    assert layering.main(str(tmp_path), "app", [], [], False) == 2
    assert "--marker" in capsys.readouterr().out

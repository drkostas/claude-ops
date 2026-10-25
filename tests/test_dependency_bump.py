"""dependency_bump: what a dependency update pull request moves, and whether a program may merge it."""
from claude_ops import dependency_bump as db

BOT = {"login": "app/dependabot", "is_bot": True}
PERSON = {"login": "someone", "is_bot": False}

GROUPED_BODY = (
    "Bumps [@vitest/mocker](https://github.com/vitest-dev/vitest/tree/HEAD/packages/mocker) to 4.1.12 "
    "and updates ancestor dependency [vitest](https://github.com/vitest-dev/vitest). These dependencies "
    "need to be updated together.\n\nUpdates `@vitest/mocker` from 4.1.11 to 4.1.12\n<details>...</details>\n"
    "Updates `vitest` from 4.1.11 to 4.1.12\n<details>...</details>\n")
SINGLE_BODY = "Bumps [tornado](https://github.com/tornadoweb/tornado) from 6.0.3 to 6.5.8.\n<details>...</details>"


def test_level_reads_semantic_versions():
    assert db.level("4.0.2", "4.0.3") == "patch"
    assert db.level("16.3.8", "16.4.1") == "minor"
    assert db.level("15.2.1", "16.0.0") == "major"
    assert db.level("v4", "v5") == "major"
    assert db.level("4.1", "4.1.2") == "patch"
    assert db.level("0.9.1", "0.10.0") == "major", "a 0.x release numbers breaking changes in the second place"
    assert db.level("0.9.1", "0.9.2") == "patch"
    assert db.level("4.0.3", "4.0.2") is None, "a move backwards is not a bump"
    assert db.level("4.0.3", "4.0.3") is None
    assert db.level("4.0.0-rc.1", "4.0.0") is None, "a pre-release is not read"
    assert db.level("abc123", "def456") is None


def test_bumps_are_read_from_title_and_body_once_each():
    assert db.bumps("Bump tornado from 6.0.3 to 6.5.8", SINGLE_BODY) == (db.Bump("tornado", "6.0.3", "6.5.8"),)
    assert db.bumps("chore(deps): bump next from 16.4.0 to 16.4.1 in /web", None) == (db.Bump("next", "16.4.0", "16.4.1"),)
    assert db.bumps("build(deps): bump @vitest/mocker and vitest in /bridge", GROUPED_BODY) == (
        db.Bump("@vitest/mocker", "4.1.11", "4.1.12"), db.Bump("vitest", "4.1.11", "4.1.12"))
    assert db.bumps("chore(deps): update dependency vitest to v4.0.3", "") == ()


def test_dependency_files():
    assert db.other_files(["web/package-lock.json", "web/package.json", "requirements-dev.txt", "poetry.lock"]) == []
    assert db.other_files(["package.json", "src/index.ts"]) == ["src/index.ts"]
    assert db.other_files([".github/workflows/ci.yml"]) == [".github/workflows/ci.yml"]
    assert db.other_files(["Dockerfile"]) == ["Dockerfile"]


def test_is_bot():
    assert db.is_bot(BOT) and db.is_bot("dependabot[bot]") and db.is_bot({"login": "renovate[bot]"})
    assert not db.is_bot(PERSON) and not db.is_bot("someone") and not db.is_bot(None)
    # any GitHub App is a bot to gh, so the flag and the `app/` prefix are not enough on their own
    assert not db.is_bot({"login": "app/some-installed-app", "is_bot": True})
    assert db.is_bot({"login": "app/some-installed-app", "is_bot": True}, bots=db.UPDATERS + ("app/some-installed-app",))


NPM_DIFF = """diff --git a/web/package.json b/web/package.json
index 1..2 100644
--- a/web/package.json
+++ b/web/package.json
@@ -10,7 +10,7 @@
   "dependencies": {
-    "next": "16.3.8",
+    "next": "16.4.1",
     "react": "19.0.0"
   },
diff --git a/web/package-lock.json b/web/package-lock.json
index 3..4 100644
--- a/web/package-lock.json
+++ b/web/package-lock.json
@@ -100,9 +100,9 @@
     "node_modules/next": {
-      "version": "16.3.8",
-      "resolved": "https://registry.example.org/next/-/next-16.3.8.tgz",
+      "version": "16.4.1",
+      "resolved": "https://registry.example.org/next/-/next-16.4.1.tgz",
       "license": "MIT"
     },
"""
TRANSITIVE = NPM_DIFF + """@@ -300,6 +300,6 @@
     "node_modules/left-pad": {
-      "version": "1.3.0",
+      "version": "2.0.0",
       "license": "MIT"
     },
"""
ADDED = NPM_DIFF + """@@ -400,0 +401,5 @@
+    "node_modules/colors": {
+      "version": "1.4.44",
+      "resolved": "https://registry.example.org/colors/-/colors-1.4.44.tgz"
+    },
"""
PY_DIFF = """diff --git a/pyproject.toml b/pyproject.toml
--- a/pyproject.toml
+++ b/pyproject.toml
@@ -8,7 +8,7 @@
 name = "myproj"
 version = "0.3.0"
 dependencies = [
-    "tornado>=6.0.3",
+    "tornado>=6.5.8",
 ]
diff --git a/uv.lock b/uv.lock
--- a/uv.lock
+++ b/uv.lock
@@ -50,8 +50,8 @@
 [[package]]
 name = "tornado"
-version = "6.0.3"
+version = "6.5.8"
 source = { registry = "https://pypi.example.org/simple" }
diff --git a/requirements.txt b/requirements.txt
--- a/requirements.txt
+++ b/requirements.txt
@@ -1,2 +1,2 @@
-tornado==6.0.3
+tornado==6.5.8
"""
YARN_CARGO_GO = """diff --git a/yarn.lock b/yarn.lock
--- a/yarn.lock
+++ b/yarn.lock
@@ -5,5 +5,5 @@
 "@scope/pkg@^1.2.0":
-  version "1.2.3"
+  version "1.2.4"
   resolved "https://registry.example.org/@scope/pkg/-/pkg-1.2.4.tgz"
diff --git a/Cargo.lock b/Cargo.lock
--- a/Cargo.lock
+++ b/Cargo.lock
@@ -20,4 +20,4 @@
 [[package]]
 name = "serde"
-version = "1.0.200"
+version = "1.0.210"
diff --git a/Cargo.toml b/Cargo.toml
--- a/Cargo.toml
+++ b/Cargo.toml
@@ -9,2 +9,2 @@
-serde = { version = "1.0.200", features = ["derive"] }
+serde = { version = "1.0.210", features = ["derive"] }
diff --git a/go.mod b/go.mod
--- a/go.mod
+++ b/go.mod
@@ -5,2 +5,2 @@
-\tgolang.org/x/text v0.14.0
+\tgolang.org/x/text v0.15.0
diff --git a/pnpm-lock.yaml b/pnpm-lock.yaml
--- a/pnpm-lock.yaml
+++ b/pnpm-lock.yaml
@@ -1,2 +1,2 @@
-  left-pad: 1.3.0
+  left-pad: 2.0.0
"""


def test_moves_reads_the_formats_it_knows_and_nothing_else():
    got = {(m.file, m.name): (m.old, m.new) for m in db.moves(NPM_DIFF + PY_DIFF + YARN_CARGO_GO)}
    assert got[("web/package.json", "next")] == ("16.3.8", "16.4.1")
    assert got[("web/package-lock.json", "next")] == ("16.3.8", "16.4.1")
    assert got[("pyproject.toml", "tornado")] == ("6.0.3", "6.5.8")
    assert got[("uv.lock", "tornado")] == ("6.0.3", "6.5.8")
    assert got[("requirements.txt", "tornado")] == ("6.0.3", "6.5.8")
    assert got[("yarn.lock", "@scope/pkg")] == ("1.2.3", "1.2.4")
    assert got[("Cargo.lock", "serde")] == ("1.0.200", "1.0.210")
    assert got[("Cargo.toml", "serde")] == ("1.0.200", "1.0.210")
    assert got[("go.mod", "golang.org/x/text")] == ("v0.14.0", "v0.15.0")
    assert not any(f == "pnpm-lock.yaml" for f, _ in got), "a format not read contributes nothing"
    assert ("pyproject.toml", "version") not in got and ("pyproject.toml", "name") not in got
    assert {(m.name, m.old, m.new) for m in db.moves(ADDED) if m.file.endswith("lock.json")} == {
        ("next", "16.3.8", "16.4.1"), ("colors", None, "1.4.44")}
    assert db.clean_version("^1.2.3") == "1.2.3" and db.clean_version(">= 1.2, <2") == "1.2" and db.clean_version("v2.0") == "v2.0"


def test_review_reads_the_diff_when_given():
    title = "chore(deps): bump next from 16.3.8 to 16.4.1 in /web"
    files = ["web/package.json", "web/package-lock.json"]
    ok = db.review(title, "", files, BOT, up_to="minor", diff=NPM_DIFF)
    assert ok.ok and "the diff moves nothing else" in ok.reason
    sneaky = db.review(title, "", files, BOT, up_to="minor", diff=TRANSITIVE)
    assert not sneaky.ok and "left-pad from 1.3.0 to 2.0.0" in sneaky.reason and "do not name" in sneaky.reason
    added = db.review(title, "", files, BOT, up_to="minor", diff=ADDED)
    assert not added.ok and "adds colors 1.4.44" in added.reason
    # a named package that moves farther in the lockfile than the text says
    far = NPM_DIFF.replace('"version": "16.4.1"', '"version": "17.0.0"')
    over = db.review(title, "", files, BOT, up_to="minor", diff=far)
    assert not over.ok and "major move in web/package-lock.json: next 16.3.8 to 17.0.0" in over.reason
    assert db.review("Bump tornado from 6.0.3 to 6.5.8", SINGLE_BODY, ["pyproject.toml", "uv.lock", "requirements.txt"],
                     BOT, up_to="minor", diff=PY_DIFF).ok
    assert db.review(title, "", files, BOT, up_to="minor").ok, "without a diff the text is the whole check, as before"


def test_review_accepts_a_bots_patch_bump_of_a_lockfile():
    v = db.review("build(deps): bump @vitest/mocker and vitest in /bridge", GROUPED_BODY,
                  ["bridge/package-lock.json", "bridge/package.json"], BOT, up_to="patch")
    assert v.ok and v.level == "patch" and len(v.bumps) == 2 and "@vitest/mocker" in v.reason


def test_review_refuses_with_the_reason():
    over = db.review("Bump tornado from 6.0.3 to 6.5.8", SINGLE_BODY, ["requirements.txt"], BOT, up_to="patch")
    assert not over.ok and over.level == "minor" and over.reason.startswith("minor bump: tornado 6.0.3 to 6.5.8")
    assert db.review("Bump tornado from 6.0.3 to 6.5.8", SINGLE_BODY, ["requirements.txt"], BOT, up_to="minor").ok
    major = db.review("Bump keras from 2.2.0 to 3.15.0", "", ["requirements.txt"], BOT, up_to="minor")
    assert not major.ok and major.level == "major" and "major bump: keras 2.2.0 to 3.15.0" in major.reason
    person = db.review("Bump x from 1.0.0 to 1.0.1", "", ["package.json"], PERSON)
    assert not person.ok and "someone, not by a bot" in person.reason
    assert db.review("Bump x from 1.0.0 to 1.0.1", "", ["package.json"], PERSON, bots_only=False).ok
    code = db.review("Bump x from 1.0.0 to 1.0.1", "", ["package.json", "src/a.py"], BOT)
    assert not code.ok and "src/a.py" in code.reason
    unread = db.review("chore(deps): update dependency vitest to v4.0.3", "", ["package.json"], BOT)
    assert not unread.ok and "no version bump could be read" in unread.reason
    back = db.review("Bump x from 1.0.1 to 1.0.0", "", ["package.json"], BOT)
    assert not back.ok and "cannot be read" in back.reason
    assert not db.review("Bump x from 1.0.0 to 1.0.1", "", ["package.json"], BOT, up_to="huge").ok


def test_review_pr_reads_gh_json():
    pr = {"title": "chore(deps): bump next from 16.3.8 to 16.4.1 in /web", "body": "",
          "author": BOT, "files": [{"path": "web/package.json"}, {"path": "web/package-lock.json"}]}
    assert db.review_pr(pr, up_to="minor").ok
    assert not db.review_pr(pr, up_to="patch").ok

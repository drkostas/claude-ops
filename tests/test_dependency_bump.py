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


def test_review_diff_checks_version_registry_and_install_scripts():
    title = "chore(deps): bump next from 16.3.8 to 16.4.1 in /web"
    files = ["web/package.json", "web/package-lock.json"]
    # the clean bump passes, with the resolved url changing version under the same registry host
    assert db.review(title, "", files, BOT, up_to="minor", diff=NPM_DIFF).ok
    # the lockfile lands on a version the text does not name, though the move is within the level
    mismatch = NPM_DIFF.replace('"version": "16.4.1"', '"version": "16.4.9"')
    v = db.review(title, "", files, BOT, up_to="minor", diff=mismatch)
    assert not v.ok and "moves next to 16.4.9" in v.reason and "name 16.4.1" in v.reason
    # the lockfile re-points the package at another registry host
    swap = NPM_DIFF.replace("https://registry.example.org/next/-/next-16.4.1.tgz",
                            "https://evil.example.net/next/-/next-16.4.1.tgz")
    v = db.review(title, "", files, BOT, up_to="minor", diff=swap)
    assert not v.ok and "points next at evil.example.net instead of registry.example.org" in v.reason
    # the lockfile newly marks the package as running an install script
    lock_script = NPM_DIFF.replace('+      "version": "16.4.1",',
                                   '+      "version": "16.4.1",\n+      "hasInstallScript": true,')
    v = db.review(title, "", files, BOT, up_to="minor", diff=lock_script)
    assert not v.ok and "next in web/package-lock.json as running an install script" in v.reason
    # a package that already ran an install script is not flagged when only its version moves
    kept = lock_script.replace('-      "version": "16.3.8",',
                               '-      "version": "16.3.8",\n-      "hasInstallScript": true,')
    assert db.review(title, "", files, BOT, up_to="minor", diff=kept).ok
    # a manifest that adds a lifecycle script under scripts is refused
    manifest_script = """diff --git a/web/package.json b/web/package.json
--- a/web/package.json
+++ b/web/package.json
@@ -10,6 +10,9 @@
   "dependencies": {
-    "next": "16.3.8"
+    "next": "16.4.1"
   },
+  "scripts": {
+    "postinstall": "node ./evil.js"
+  }
 }
"""
    v = db.review(title, "", ["web/package.json"], BOT, up_to="minor", diff=manifest_script)
    assert not v.ok and "adds the install script postinstall in web/package.json" in v.reason


# A real npm package-lock.json diff for a next 16.4.1 to 16.4.5 bump. next's own lockfile entry pins
# @next/env and eight @next/swc-* packages to exactly 16.4.5, so each is an expected co-move the title
# does not name. The integrity lines are kept as the parser sees them and reads nothing from them.
NEXT_PINS = r"""diff --git a/web/package-lock.json b/web/package-lock.json
index 4d44bba..0f7534f 100644
--- a/web/package-lock.json
+++ b/web/package-lock.json
@@ -12,7 +12,7 @@
         "@neondatabase/serverless": "^0.10.0",
         "ai": "^6.0.282",
         "macro-engine-core": "^0.3.0",
-        "next": "^16.4.1",
+        "next": "^16.4.5",
         "postgres": "^3.4.9",
         "react": "^19.1.0",
         "react-dom": "^19.1.0",
@@ -2538,9 +2538,9 @@
       }
     },
     "node_modules/@next/env": {
-      "version": "16.4.1",
-      "resolved": "https://registry.npmjs.org/@next/env/-/env-16.4.1.tgz",
-      "integrity": "sha512-5sUUrrSvs/2/2LnLoesybMGx12EtPB46fdZ6RhAw0XDGSPxU2LFyQBB7yuEiOFFJPKmBY6+q2jTr1/AMbnp0/g==",
+      "version": "16.4.5",
+      "resolved": "https://registry.npmjs.org/@next/env/-/env-16.4.5.tgz",
+      "integrity": "sha512-JwSPwpdkZL5VMl2jA/q+JI8MZdweAL3xmw5/tb9y/TLtmS0YXZnmYDzSihwk8xTj1OL+WM53gTqaj96yuLp6Xw==",
       "license": "MIT"
     },
     "node_modules/@next/eslint-plugin-next": {
@@ -2587,9 +2587,9 @@
       }
     },
     "node_modules/@next/swc-darwin-arm64": {
-      "version": "16.4.1",
-      "resolved": "https://registry.npmjs.org/@next/swc-darwin-arm64/-/swc-darwin-arm64-16.4.1.tgz",
-      "integrity": "sha512-U+hQ5Gj3fpzf4fveTIF3dpUFYhaSW8vHhdAnbiIomzXzg7boreYFgBqprepXFtW4sw+aTttiNmg0gew/iFNhbg==",
+      "version": "16.4.5",
+      "resolved": "https://registry.npmjs.org/@next/swc-darwin-arm64/-/swc-darwin-arm64-16.4.5.tgz",
+      "integrity": "sha512-W5TniBzWKASbNNwHZ/S/f1MxQUXU/LdjklVMWA0Ujbfl7SZnUzFls45hO0i/sF7Bx1jJBWp7WWmoQajXtX/O8A==",
       "cpu": [
         "arm64"
       ],
@@ -2603,9 +2603,9 @@
       }
     },
     "node_modules/@next/swc-darwin-x64": {
-      "version": "16.4.1",
-      "resolved": "https://registry.npmjs.org/@next/swc-darwin-x64/-/swc-darwin-x64-16.4.1.tgz",
-      "integrity": "sha512-/hqC3V1XLUjhMASf/WridKdO9FXoEgN3HBWmtKbj00MPgnltutfraWJ3bYTpCRIgEIpbjJyOVLpYgAhh/BkdcQ==",
+      "version": "16.4.5",
+      "resolved": "https://registry.npmjs.org/@next/swc-darwin-x64/-/swc-darwin-x64-16.4.5.tgz",
+      "integrity": "sha512-uWq4qkHapBDS1oFltWS4YxTb9pLHAtLNcpbuK6SmRsebLYZqpBML/j/C0op+50JYGTpZgVL/DWNt/6oWlamrrw==",
       "cpu": [
         "x64"
       ],
@@ -2619,9 +2619,9 @@
       }
     },
     "node_modules/@next/swc-linux-arm64-gnu": {
-      "version": "16.4.1",
-      "resolved": "https://registry.npmjs.org/@next/swc-linux-arm64-gnu/-/swc-linux-arm64-gnu-16.4.1.tgz",
-      "integrity": "sha512-GHzncHbcaS9g95kcvTNtKQW3aCLoR+xc3Jm7qrkpk1R+D2BjMHIPyvQGRPKCdMX95BsH/421jBUK06q9tEwzjw==",
+      "version": "16.4.5",
+      "resolved": "https://registry.npmjs.org/@next/swc-linux-arm64-gnu/-/swc-linux-arm64-gnu-16.4.5.tgz",
+      "integrity": "sha512-FdRcyOx5u5P51mlAqLB9EXdd8jZO5fsdHtk4BAQSkqOywCsbTNN/rmw+jO7pnmjNdAtu2AG46NzbCL2FPDrrgw==",
       "cpu": [
         "arm64"
       ],
@@ -2638,9 +2638,9 @@
       }
     },
     "node_modules/@next/swc-linux-arm64-musl": {
-      "version": "16.4.1",
-      "resolved": "https://registry.npmjs.org/@next/swc-linux-arm64-musl/-/swc-linux-arm64-musl-16.4.1.tgz",
-      "integrity": "sha512-1SuJyDxW/PR9dHJat/ZRgcYLhuSZ+1psP4DlKb2tKIuXbjygqpLrJWRSq+onZQBv8LcmU67EIrmb6c3XTaJLjw==",
+      "version": "16.4.5",
+      "resolved": "https://registry.npmjs.org/@next/swc-linux-arm64-musl/-/swc-linux-arm64-musl-16.4.5.tgz",
+      "integrity": "sha512-cyvmmgtRXR8aGVT86wi9Py53IlfyCx7P2fZxuAXAoTummDnhKtUyjrMeXfapqp4qgD+gT5RqyGD/VfA16jEK7Q==",
       "cpu": [
         "arm64"
       ],
@@ -2657,9 +2657,9 @@
       }
     },
     "node_modules/@next/swc-linux-x64-gnu": {
-      "version": "16.4.1",
-      "resolved": "https://registry.npmjs.org/@next/swc-linux-x64-gnu/-/swc-linux-x64-gnu-16.4.1.tgz",
-      "integrity": "sha512-Istng1hMGp9TGO4lUqeKLd70MPN/OOkucVTP2Nmj+gLkwBgmPqUw8CJYBCC7nbl/B/23bBCcjCI5Hsh6hazC1A==",
+      "version": "16.4.5",
+      "resolved": "https://registry.npmjs.org/@next/swc-linux-x64-gnu/-/swc-linux-x64-gnu-16.4.5.tgz",
+      "integrity": "sha512-hPc+Fl+N22DAvqZHq559NmamXboOX5Lu8I5Wl0DezfrhTk1Zz21NQeYoZDmNI/NjTcrdsVxM72Fzn2lI+Q+vAw==",
       "cpu": [
         "x64"
       ],
@@ -2676,9 +2676,9 @@
       }
     },
     "node_modules/@next/swc-linux-x64-musl": {
-      "version": "16.4.1",
-      "resolved": "https://registry.npmjs.org/@next/swc-linux-x64-musl/-/swc-linux-x64-musl-16.4.1.tgz",
-      "integrity": "sha512-aSC9wMNiZRRFVpPkPWBzRwLQxIGm/CiQcXyFF1XNcgodb+uMxdk7aSSGGpDXCbnvZdf4itjHxpOd5h7i7zaiYw==",
+      "version": "16.4.5",
+      "resolved": "https://registry.npmjs.org/@next/swc-linux-x64-musl/-/swc-linux-x64-musl-16.4.5.tgz",
+      "integrity": "sha512-gbACYE8IP/jlprNTpiWXHb8N9CofOzpnEwH2SrIz1dUbiJf1FbDssDupCAPeP1Taxe8mgG20sGZamJI7KFOdZw==",
       "cpu": [
         "x64"
       ],
@@ -2695,9 +2695,9 @@
       }
     },
     "node_modules/@next/swc-win32-arm64-msvc": {
-      "version": "16.4.1",
-      "resolved": "https://registry.npmjs.org/@next/swc-win32-arm64-msvc/-/swc-win32-arm64-msvc-16.4.1.tgz",
-      "integrity": "sha512-t/myvwFDlypMUjRvbBf8qLPyuaB0iXYYdBtwMt6DM6f+60wtGr4bSYholdPqNeV5VUXZhrpMwfza829kmhS3iA==",
+      "version": "16.4.5",
+      "resolved": "https://registry.npmjs.org/@next/swc-win32-arm64-msvc/-/swc-win32-arm64-msvc-16.4.5.tgz",
+      "integrity": "sha512-+8gj++ZMo8H20Xh2ijjNqyFk5LPLw0AC4Fs4Tq/G94rjjQXhOjN4jgSWsWKPFsyY26hfIeemJIwKtMxOsp5GBA==",
       "cpu": [
         "arm64"
       ],
@@ -2711,9 +2711,9 @@
       }
     },
     "node_modules/@next/swc-win32-x64-msvc": {
-      "version": "16.4.1",
-      "resolved": "https://registry.npmjs.org/@next/swc-win32-x64-msvc/-/swc-win32-x64-msvc-16.4.1.tgz",
-      "integrity": "sha512-6ICqPo9awCkrtYNPhwkx1uU7rlFWrZRrIboDHS2LU+jv088UBYQtmJ25h6pCp27/+FZjF6BQ4Te4ErugyMeuBg==",
+      "version": "16.4.5",
+      "resolved": "https://registry.npmjs.org/@next/swc-win32-x64-msvc/-/swc-win32-x64-msvc-16.4.5.tgz",
+      "integrity": "sha512-qNZj66pYXnaWLiF9R9GUbfBdrCv7kC6TaZfIwB2KH5WPc/8pEM3nVOj8hpQhWJU8S94rlH5MPDcX15+RrgUhiw==",
       "cpu": [
         "x64"
       ],
@@ -9684,12 +9684,12 @@
       }
     },
     "node_modules/next": {
-      "version": "16.4.1",
-      "resolved": "https://registry.npmjs.org/next/-/next-16.4.1.tgz",
-      "integrity": "sha512-uc657V62E34fOI6W5NtG9IzwsPcms0Z60grz2MJGtglgD+pTFnNt4Yw2PzaU622tfX4vhrDgLZzicE2YlhI20Q==",
+      "version": "16.4.5",
+      "resolved": "https://registry.npmjs.org/next/-/next-16.4.5.tgz",
+      "integrity": "sha512-rmdfsWosjTUO+6R0S3KokoIk6Kj0sWDRTe86tZHdqaWH/oXq/udX3/4xb+YQi7aWbyEFoglDU54kaifLCdXbBw==",
       "license": "MIT",
       "dependencies": {
-        "@next/env": "16.4.1",
+        "@next/env": "16.4.5",
         "@swc/helpers": "0.5.23",
         "baseline-browser-mapping": "^2.9.19",
         "caniuse-lite": "^1.0.30001579",
@@ -9703,14 +9703,14 @@
         "node": ">=20.9.0"
       },
       "optionalDependencies": {
-        "@next/swc-darwin-arm64": "16.4.1",
-        "@next/swc-darwin-x64": "16.4.1",
-        "@next/swc-linux-arm64-gnu": "16.4.1",
-        "@next/swc-linux-arm64-musl": "16.4.1",
-        "@next/swc-linux-x64-gnu": "16.4.1",
-        "@next/swc-linux-x64-musl": "16.4.1",
-        "@next/swc-win32-arm64-msvc": "16.4.1",
-        "@next/swc-win32-x64-msvc": "16.4.1",
+        "@next/swc-darwin-arm64": "16.4.5",
+        "@next/swc-darwin-x64": "16.4.5",
+        "@next/swc-linux-arm64-gnu": "16.4.5",
+        "@next/swc-linux-arm64-musl": "16.4.5",
+        "@next/swc-linux-x64-gnu": "16.4.5",
+        "@next/swc-linux-x64-musl": "16.4.5",
+        "@next/swc-win32-arm64-msvc": "16.4.5",
+        "@next/swc-win32-x64-msvc": "16.4.5",
         "sharp": "^0.35.5"
       },
       "peerDependencies": {
diff --git a/web/package.json b/web/package.json
index 1d3002a..3b95f5d 100644
--- a/web/package.json
+++ b/web/package.json
@@ -21,7 +21,7 @@
     "@neondatabase/serverless": "^0.10.0",
     "ai": "^6.0.282",
     "macro-engine-core": "^0.3.0",
-    "next": "^16.4.1",
+    "next": "^16.4.5",
     "postgres": "^3.4.9",
     "react": "^19.1.0",
     "react-dom": "^19.1.0",
"""


def test_review_accepts_a_frameworks_own_pinned_subpackages():
    # (a) the real next 16.4.1 to 16.4.5 lockfile hunk passes: nine @next/* co-moves, each pinned
    # to exactly the version it lands on by next's own entry.
    title = "chore(deps): bump next from 16.4.1 to 16.4.5 in /web"
    files = ["web/package.json", "web/package-lock.json"]
    comoves = [m for m in db.moves(NEXT_PINS) if m.name.startswith("@next/")]
    assert len(comoves) == 9, "the diff moves @next/env and eight @next/swc-* packages next does not name"
    v = db.review(title, "", files, BOT, up_to="minor", diff=NEXT_PINS)
    assert v.ok, v.reason


def test_review_refuses_an_unpinned_scope_sibling():
    # (b) an @next/* package that next does not pin is refused: drop one swc from next's
    # optionalDependencies map while its own entry still moves.
    title = "chore(deps): bump next from 16.4.1 to 16.4.5 in /web"
    files = ["web/package.json", "web/package-lock.json"]
    unpinned = NEXT_PINS.replace('-        "@next/swc-win32-x64-msvc": "16.4.1",\n', "")
    unpinned = unpinned.replace('+        "@next/swc-win32-x64-msvc": "16.4.5",\n', "")
    v = db.review(title, "", files, BOT, up_to="minor", diff=unpinned)
    assert not v.ok and "@next/swc-win32-x64-msvc" in v.reason and "do not name" in v.reason, v.reason


def test_review_refuses_a_comove_to_another_version():
    # (c) a co-move to a version other than the pinned one is refused: next pins @next/env to 16.4.5
    # but its entry lands on 16.4.6.
    title = "chore(deps): bump next from 16.4.1 to 16.4.5 in /web"
    files = ["web/package.json", "web/package-lock.json"]
    off = NEXT_PINS.replace(
        '+      "version": "16.4.5",\n+      "resolved": "https://registry.npmjs.org/@next/env/-/env-16.4.5.tgz",',
        '+      "version": "16.4.6",\n+      "resolved": "https://registry.npmjs.org/@next/env/-/env-16.4.5.tgz",')
    v = db.review(title, "", files, BOT, up_to="minor", diff=off)
    assert not v.ok and "@next/env to 16.4.6" in v.reason and "pins it to 16.4.5" in v.reason, v.reason


def test_review_refuses_a_range_pinned_comove():
    # (d) a pin with a range (^ or ~) is refused: next lists @next/env with a caret range, not exact.
    title = "chore(deps): bump next from 16.4.1 to 16.4.5 in /web"
    files = ["web/package.json", "web/package-lock.json"]
    ranged = NEXT_PINS.replace('+        "@next/env": "16.4.5",', '+        "@next/env": "^16.4.5",')
    v = db.review(title, "", files, BOT, up_to="minor", diff=ranged)
    assert not v.ok and "@next/env to 16.4.5" in v.reason and "range ^16.4.5" in v.reason, v.reason


def test_review_refuses_a_pinned_comove_that_changes_registry_host():
    # (e) a pinned co-move whose resolved host changes is refused: @next/env is pinned to 16.4.5 but
    # its entry is re-pointed at another host.
    title = "chore(deps): bump next from 16.4.1 to 16.4.5 in /web"
    files = ["web/package.json", "web/package-lock.json"]
    swap = NEXT_PINS.replace("https://registry.npmjs.org/@next/env/-/env-16.4.5.tgz",
                             "https://evil.example.net/@next/env/-/env-16.4.5.tgz")
    v = db.review(title, "", files, BOT, up_to="minor", diff=swap)
    assert not v.ok and "points @next/env at evil.example.net" in v.reason, v.reason


def test_review_refuses_a_pinned_comove_that_adds_an_install_script():
    # (f) a pinned co-move that adds an install script is refused: @next/env is pinned to 16.4.5 but
    # its entry is newly marked to run one.
    title = "chore(deps): bump next from 16.4.1 to 16.4.5 in /web"
    files = ["web/package.json", "web/package-lock.json"]
    script = NEXT_PINS.replace(
        '+      "version": "16.4.5",\n+      "resolved": "https://registry.npmjs.org/@next/env/-/env-16.4.5.tgz",',
        '+      "version": "16.4.5",\n+      "hasInstallScript": true,\n+      "resolved": "https://registry.npmjs.org/@next/env/-/env-16.4.5.tgz",')
    v = db.review(title, "", files, BOT, up_to="minor", diff=script)
    assert not v.ok and "@next/env in web/package-lock.json as running an install script" in v.reason, v.reason

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

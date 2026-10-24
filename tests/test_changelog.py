import json
import subprocess

import pytest

from claude_ops import changelog


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=a@example.org", "-c", "user.name=A",
                           *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    (r / "a").write_text("1")
    git(r, "add", "a")
    git(r, "commit", "-qm", "start")
    return r


def pull_request(repo, n, branch, title, files=1):
    git(repo, "checkout", "-qb", branch)
    for i in range(files):   # several commits inside the pull request, none of them listed
        (repo / f"{branch}-{i}").write_text(str(i))
        git(repo, "add", f"{branch}-{i}")
        git(repo, "commit", "-qm", f"work {i}")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", branch, "-m", f"Merge pull request #{n} from owner/{branch}",
        "-m", f"{title}\n\nmore words")


def test_merges_squashes_and_direct_commits_oldest_first(repo):
    start = git(repo, "rev-parse", "HEAD")
    pull_request(repo, 7, "first", "Read the config once", files=3)
    (repo / "b").write_text("2")
    git(repo, "add", "b")
    git(repo, "commit", "-qm", "Fix the parser (#8)")
    (repo / "c").write_text("3")
    git(repo, "add", "c")
    git(repo, "commit", "-qm", "a commit pushed straight to main")
    pull_request(repo, 9, "second", "Retry on a timeout")
    got = changelog.merged(start, "HEAD", str(repo))
    assert [changelog.line(c) for c in got] == [
        "#7 Read the config once", "#8 Fix the parser", "a commit pushed straight to main",
        "#9 Retry on a timeout"]
    assert got[0].branch == "owner/first" and got[2].number is None


def test_an_empty_range_and_an_unknown_commit(repo):
    assert changelog.merged("HEAD", "HEAD", str(repo)) == []
    with pytest.raises(ValueError):
        changelog.merged("0" * 40, "HEAD", str(repo))


def test_a_merge_with_no_body_is_named_by_its_branch():
    log = f"abc{changelog.FIELD}Merge pull request #3 from owner/topic{changelog.FIELD}{changelog.RECORD}"
    (c,) = changelog.parse(log)
    assert (c.number, c.title) == (3, "owner/topic")


def test_web_url():
    assert changelog.web_url("git@github.com:owner/app.git") == "https://github.com/owner/app"
    assert changelog.web_url("https://github.com/owner/app") == "https://github.com/owner/app"
    assert changelog.web_url("https://example.org/owner/app.git") is None


def test_cli(repo, capsys):
    start = git(repo, "rev-parse", "HEAD")
    pull_request(repo, 4, "x", "Add the export")
    assert changelog.main([start, "HEAD", "--repo", str(repo), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["title"] == "Add the export"
    assert changelog.main(["nope", "HEAD", "--repo", str(repo)]) == 2

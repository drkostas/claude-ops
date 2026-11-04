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
    stamps = git(repo, "log", "--first-parent", "--format=%cI", f"{start}..HEAD").splitlines()
    assert [c.committed for c in got] == list(reversed(stamps))
    assert all(changelog.ISO_TIME.match(c.committed) for c in got)


def test_an_empty_range_and_an_unknown_commit(repo):
    assert changelog.merged("HEAD", "HEAD", str(repo)) == []
    with pytest.raises(ValueError):
        changelog.merged("0" * 40, "HEAD", str(repo))


def test_a_merge_with_no_body_is_named_by_its_branch():
    log = f"abc{changelog.FIELD}Merge pull request #3 from owner/topic{changelog.FIELD}{changelog.RECORD}"
    (c,) = changelog.parse(log)
    assert (c.number, c.title, c.committed) == (3, "owner/topic", None)

def test_the_commit_time_is_read_when_the_log_carries_it():
    F, R = changelog.FIELD, changelog.RECORD
    log = (f"abc{F}Merge pull request #3 from owner/topic{F}Read it once{F}2026-01-02T03:04:05+02:00{R}"
           f"def{F}Fix the parser (#4){F}{F}2026-01-02T04:00:00Z{R}"
           f"0aa{F}pushed straight{F}a body{F}with a separator{F}2026-01-03T00:00:00-05:00{R}"
           f"0bb{F}an older log{F}no time here{R}")
    a, b, c, d = changelog.parse(log)
    assert (a.number, a.title, a.committed) == (3, "Read it once", "2026-01-02T03:04:05+02:00")
    assert (b.number, b.title, b.committed) == (4, "Fix the parser", "2026-01-02T04:00:00Z")
    assert (c.title, c.committed) == ("pushed straight", "2026-01-03T00:00:00-05:00")
    assert (d.title, d.committed) == ("an older log", None)


def test_web_url():
    assert changelog.web_url("git@github.com:owner/app.git") == "https://github.com/owner/app"
    assert changelog.web_url("https://github.com/owner/app") == "https://github.com/owner/app"
    assert changelog.web_url("https://example.org/owner/app.git") is None


def test_cli(repo, capsys):
    start = git(repo, "rev-parse", "HEAD")
    pull_request(repo, 4, "x", "Add the export")
    assert changelog.main([start, "HEAD", "--repo", str(repo), "--json"]) == 0
    first = json.loads(capsys.readouterr().out)[0]
    assert first["title"] == "Add the export" and changelog.ISO_TIME.match(first["committed"])
    assert changelog.main(["nope", "HEAD", "--repo", str(repo)]) == 2

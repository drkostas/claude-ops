"""fork_guard: scheduled workflows that act on the repository without a fork guard."""
import yaml

from claude_ops import fork_guard


# A scheduled job that comments on an issue, with no guard. This is the shape that breaks on forks.
UNGUARDED = """
name: Watch
on:
  schedule:
    - cron: "17 6 * * 1"
  workflow_dispatch:
jobs:
  watch:
    runs-on: ubuntu-latest
    steps:
      - name: comment
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          gh issue comment 1 --repo "$GITHUB_REPOSITORY" --body-file comment.md
"""

# The same job with the guard on the job, so only the original repository runs it.
GUARDED = """
name: Watch
on:
  schedule:
    - cron: "17 6 * * 1"
jobs:
  watch:
    if: github.repository == 'owner/name'
    runs-on: ubuntu-latest
    steps:
      - run: gh issue comment 1 --repo "$GITHUB_REPOSITORY" --body-file comment.md
"""

# The guard on the acting step instead of the job also clears it.
STEP_GUARDED = """
name: Watch
on: workflow_dispatch
jobs:
  watch:
    runs-on: ubuntu-latest
    steps:
      - run: echo hello
      - if: github.repository == 'owner/name'
        run: gh pr comment 1 --repo "$GITHUB_REPOSITORY" --body hi
"""

# A pull_request workflow runs in a fork by design, so it is not this check's concern.
NOT_SCHEDULED = """
name: Label
on:
  pull_request:
jobs:
  label:
    runs-on: ubuntu-latest
    steps:
      - run: gh issue edit 1 --add-label triage
"""

# An actions/github-script step that comments, on a schedule, with no guard.
GITHUB_SCRIPT = """
name: Sweep
on:
  schedule:
    - cron: "0 0 * * *"
jobs:
  sweep:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/github-script@v7
        with:
          script: |
            await github.rest.issues.createComment({issue_number: 1, owner, repo, body: 'hi'})
"""

# A scheduled job that does not touch issues, pull requests or comments is left alone.
NO_REPO_ACTION = """
name: Nightly
on:
  schedule:
    - cron: "0 3 * * *"
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: make release && gh release upload v1 dist/*
"""

# gh api to a REST path under issues, on a schedule, with no guard.
API_PATH = """
name: Api
on:
  schedule:
    - cron: "0 0 * * *"
jobs:
  ask:
    runs-on: ubuntu-latest
    steps:
      - run: gh api "repos/$GITHUB_REPOSITORY/issues/1/comments" -f body=hi
"""


def write(tmp_path, name, text):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_unguarded_scheduled_job_is_flagged():
    assert fork_guard.unguarded(yaml.safe_load(UNGUARDED)) == ["watch"]


def test_the_job_guard_clears_it():
    assert fork_guard.unguarded(yaml.safe_load(GUARDED)) == []


def test_pyyaml_reads_on_as_the_boolean_true_and_detection_still_works():
    doc = yaml.safe_load(UNGUARDED)
    assert "on" not in doc and doc.get(True) is not None   # the gotcha this code guards against
    assert fork_guard.unguarded(doc) == ["watch"]


def test_a_guard_on_the_acting_step_clears_the_job():
    assert fork_guard.unguarded(yaml.safe_load(STEP_GUARDED)) == []


def test_a_pull_request_workflow_is_not_flagged():
    assert fork_guard.unguarded(yaml.safe_load(NOT_SCHEDULED)) == []


def test_a_github_script_comment_is_flagged():
    assert fork_guard.unguarded(yaml.safe_load(GITHUB_SCRIPT)) == ["sweep"]


def test_a_scheduled_job_that_does_not_touch_the_repo_is_not_flagged():
    assert fork_guard.unguarded(yaml.safe_load(NO_REPO_ACTION)) == []


def test_a_gh_api_path_is_flagged():
    assert fork_guard.unguarded(yaml.safe_load(API_PATH)) == ["ask"]


def test_scan_a_folder_reports_only_the_unguarded(tmp_path):
    write(tmp_path, "guarded.yml", GUARDED)
    write(tmp_path, "unguarded.yml", UNGUARDED)
    out = fork_guard.scan(tmp_path)
    assert out["hits"] == [{"file": str(tmp_path / "unguarded.yml"), "job": "watch"}]
    assert out["unreadable"] == []


def test_scan_a_repository_root_finds_github_workflows(tmp_path):
    write(tmp_path, ".github/workflows/watch.yml", UNGUARDED)
    out = fork_guard.scan(tmp_path)
    assert [h["job"] for h in out["hits"]] == ["watch"]


def test_an_unreadable_workflow_is_reported_not_a_hit(tmp_path):
    write(tmp_path, "bad.yml", "on: [unterminated\njobs:\n")
    out = fork_guard.scan(tmp_path)
    assert out["hits"] == []
    assert len(out["unreadable"]) == 1 and out["unreadable"][0]["file"].endswith("bad.yml")


def test_main_exit_one_on_a_hit_and_zero_when_clean(tmp_path, capsys):
    write(tmp_path, "unguarded.yml", UNGUARDED)
    assert fork_guard.main([str(tmp_path)]) == 1
    assert "unguarded.yml::watch" in capsys.readouterr().out
    clean = tmp_path / "clean"
    write(clean, "guarded.yml", GUARDED)
    assert fork_guard.main([str(clean)]) == 0
    assert "no unguarded" in capsys.readouterr().out


def test_main_json(tmp_path, capsys):
    write(tmp_path, "unguarded.yml", UNGUARDED)
    assert fork_guard.main([str(tmp_path), "--json"]) == 1
    import json
    data = json.loads(capsys.readouterr().out)
    assert data["hits"][0]["job"] == "watch"

import datetime as dt
import json
import os

from claude_ops import runner_diag as rd

T0 = dt.datetime(2030, 1, 2, 10, 0, 0, tzinfo=dt.timezone.utc)


def _stamp(at: dt.datetime) -> str:
    return at.strftime("[%Y-%m-%d %H:%M:%SZ INFO StepsRunner] ")


def _telemetry(start: dt.datetime, end: dt.datetime, result: str, action: str = "run") -> list[str]:
    return [_stamp(end).replace("StepsRunner", "ExecutionContext") + "Publish step telemetry for current step {",
            f'  "action": "{action}",', f'  "result": "{result}",',
            f'  "executionTimeInSeconds": {int((end - start).total_seconds())},',
            f'  "startTime": "{start.strftime("%Y-%m-%dT%H:%M:%S")}.123456Z",',
            f'  "finishTime": "{end.strftime("%Y-%m-%dT%H:%M:%S")}.654321Z"', "}."]


def worker_log(steps, *, job="Tests", workflow="CI", repository="someone/app", run_id="111", result="Succeeded",
               start=T0) -> str:
    """A Worker log as a runner writes it: the job's message, then for each step (name, seconds, result) its
    processing and start lines and its telemetry. A result of "skipped" is a step its condition skipped, and
    seconds None is a step still running."""
    lines = ["[2030-01-02 09:59:58Z INFO Worker] Job message:", "{",
             '  "contextData": {', '    "github": {', '      "d": [',
             f'        {{"k": "repository", "v": "{repository}"}},', f'        {{"k": "workflow", "v": "{workflow}"}},',
             f'        {{"k": "run_id", "v": "{run_id}"}},', '        {"k": "run_attempt", "v": "1"}',
             "      ]", "    }", "  },", f'  "jobDisplayName": "{job}",', '  "jobName": "__default"', "}"]
    lines += _telemetry(start - dt.timedelta(seconds=2), start, "succeeded", "Pre Job Hook")   # no step's own
    at = start
    for name, seconds, res in steps:
        lines.append(_stamp(at) + f"Processing step: DisplayName='{name}'")
        if res == "skipped":
            lines.append(_stamp(at) + "Skipping step due to condition evaluation.")
            continue
        lines.append(_stamp(at) + "Starting the step.")
        if seconds is None:
            break
        end = at + dt.timedelta(seconds=seconds)
        lines.append(_stamp(end) + "Step result: ")
        lines += _telemetry(at, end, res)
        at = end
    if result:
        lines.append(_stamp(at).replace("StepsRunner", "JobRunner") + f"Job result after all job steps finish: {result}")
    return "\n".join(lines) + "\n"


def _write(folder, name, text, mtime: dt.datetime):
    p = folder / name
    p.write_text(text)
    os.utime(p, (mtime.timestamp(), mtime.timestamp()))
    return p


def test_parse_reads_the_job_its_steps_and_its_result():
    job = rd.parse(worker_log([("Set up job", 5, "succeeded"), ("Lint", 0, "skipped"),
                               ("Run tests", 1800, "failed"), ("Post checkout", 1, "succeeded")], result="Failed"))
    assert (job.repository, job.workflow, job.name, job.run_id, job.attempt, job.result) == (
        "someone/app", "CI", "Tests", "111", "1", "Failed")
    assert [(s.name, s.seconds(), s.result) for s in job.steps] == [
        ("Set up job", 5, "succeeded"), ("Run tests", 1800, "failed"), ("Post checkout", 1, "succeeded")]
    assert job.steps[0].started == T0 and job.key == ("someone/app", "CI", "Tests")


def test_a_running_step_counts_until_now_and_a_job_has_no_result_yet():
    job = rd.parse(worker_log([("Set up job", 5, "succeeded"), ("Run tests", None, None)], result=None))
    assert job.result is None and job.steps[-1].finished is None
    assert job.steps[-1].seconds() is None
    assert job.steps[-1].seconds(T0 + dt.timedelta(seconds=605)) == 600


def test_slower_names_a_step_far_past_its_green_time_and_only_that():
    green = rd.parse(worker_log([("Set up job", 5, "succeeded"), ("Build", 100, "succeeded"),
                                 ("Run tests", 1800, "succeeded"), ("Upload", 60, "succeeded"),
                                 ("Upload", 60, "succeeded")]))
    job = rd.parse(worker_log([("Set up job", 6, "succeeded"), ("Build", 400, "succeeded"),
                               ("Run tests", 4400, "canceled"), ("Upload", 60, "succeeded"),
                               ("Upload", 900, "succeeded"), ("New step", 5000, "succeeded")], result="Canceled"))
    got = rd.slower(job, green)
    # Build ran four times longer but only 300 s more; New step has no green time; the second Upload is
    # matched with the green run's second Upload
    assert got == [{"step": "Run tests", "seconds": 4400, "green_seconds": 1800, "result": "canceled"},
                   {"step": "Upload", "seconds": 900, "green_seconds": 60, "result": "succeeded"}]
    assert rd.slower(job, None) == []
    assert rd.slower(job, green, factor=3) == [{"step": "Upload", "seconds": 900, "green_seconds": 60,
                                                "result": "succeeded"}]


def test_an_open_step_counts_until_now_while_the_log_is_written_and_until_its_last_write_after(tmp_path):
    green = rd.parse(worker_log([("Run tests", 600, "succeeded")]))
    path = _write(tmp_path, "Worker_running.log", worker_log([("Run tests", None, None)], result=None),
                  T0 + dt.timedelta(seconds=1500))
    job = rd.read(path)
    assert job.written == T0 + dt.timedelta(seconds=1500)
    live = T0 + dt.timedelta(seconds=1560)              # written a minute ago: the worker still runs
    assert rd.slower(job, green, now=live) == [{"step": "Run tests", "seconds": 1560, "green_seconds": 600,
                                                "result": None}]
    later = T0 + dt.timedelta(days=1)                   # silent for a day: its worker ended mid-step
    assert rd.slower(job, green, now=later) == [{"step": "Run tests", "seconds": 1500, "green_seconds": 600,
                                                 "result": None}]
    assert rd.slower(job, green, now=T0 + dt.timedelta(seconds=1000), margin_s=10_000) == []


def test_last_green_takes_the_newest_older_success_of_the_same_job(tmp_path):
    ok = [("Run tests", 600, "succeeded")]
    _write(tmp_path, "Worker_a.log", worker_log(ok, run_id="1"), T0)
    _write(tmp_path, "Worker_b.log", worker_log(ok, run_id="2"), T0 + dt.timedelta(hours=1))
    _write(tmp_path, "Worker_c.log", worker_log(ok, run_id="3", result="Failed"), T0 + dt.timedelta(hours=2))
    _write(tmp_path, "Worker_d.log", worker_log(ok, run_id="4", job="Other"), T0 + dt.timedelta(hours=3))
    _write(tmp_path, "Worker_e.log", worker_log(ok, run_id="5", workflow="Deploy"), T0 + dt.timedelta(hours=3))
    me = _write(tmp_path, "Worker_f.log", worker_log([("Run tests", None, None)], run_id="6", result=None),
                T0 + dt.timedelta(hours=4))
    _write(tmp_path, "Worker_g.log", worker_log(ok, run_id="7"), T0 + dt.timedelta(hours=5))   # newer than it
    job = rd.read(me)
    green = rd.last_green(tmp_path, job)
    assert green is not None and green.run_id == "2"
    assert rd.last_green(tmp_path, job, limit=3) is None   # the three newest older logs are not the job's green
    recent = [p.name for p in rd.logs(tmp_path, since=T0 + dt.timedelta(hours=3))]
    assert recent[:2] == ["Worker_g.log", "Worker_f.log"] and sorted(recent[2:]) == ["Worker_d.log", "Worker_e.log"]


def test_cli_prints_recent_jobs_with_their_slow_steps(tmp_path, capsys):
    now = dt.datetime.now(dt.timezone.utc)
    _write(tmp_path, "Worker_old.log", worker_log([("Run tests", 600, "succeeded")], run_id="1"),
           now - dt.timedelta(hours=10))
    _write(tmp_path, "Worker_new.log", worker_log([("Run tests", 2000, "failed")], run_id="2", result="Failed"),
           now - dt.timedelta(minutes=5))
    assert rd.main([str(tmp_path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [(j["log"], j["run_id"], j["result"], j["green_log"]) for j in out] == [
        ("Worker_new.log", "2", "Failed", "Worker_old.log")]
    assert out[0]["slower"] == [{"step": "Run tests", "seconds": 2000, "green_seconds": 600, "result": "failed"}]
    assert rd.main([str(tmp_path), "--hours", "24", "--factor", "5"]) == 0
    assert all(j["slower"] == [] for j in json.loads(capsys.readouterr().out))

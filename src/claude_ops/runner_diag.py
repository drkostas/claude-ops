"""Read a GitHub Actions self-hosted runner's diagnostic logs, and name the steps that ran far too long.

A self-hosted runner writes one Worker log per job in its `_diag` folder (`Worker_<time>-utc.log`). The
log holds the job's message (its repository, workflow, job name, run id and attempt), a line as each
step is processed and starts, a telemetry block with each finished step's result and times, and the
job's result once every step has finished. A step that runs far longer than it did in the job's last
successful run is often the machine being slow, not the change being tested, and nothing on the run's
page says so. A timeout then reads as a failing test.

    claude-ops runner-diag <diag folder> [--hours 6] [--factor 2] [--margin 600]

prints, for every Worker log written in the last hours, the job and the steps that ran longer than
`factor` times the same step in the job's last successful run and at least `margin` seconds more, as
JSON. `read(path)` gives a `Job` with its `Step`s, `last_green(folder, job)` finds the job's last
successful run among the older logs, and `slower(job, green)` compares the two.

A step still running counts by the time it has run so far, measured to `now`. A live worker writes
its log every few seconds, so a job with no result whose log has not been written for `SILENT_S` is one
whose worker ended mid-step, and its open step counts only until the log's last write.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: a log line's start: "[2026-11-15 23:35:17Z INFO StepsRunner] ..."
LINE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})Z (\w+) ([\w.]+)\] (.*)$")
PROCESSING = re.compile(r"Processing step: DisplayName='(.*)'$")
TELEMETRY = "Publish step telemetry for current step {"
JOB_RESULT = re.compile(r"Job result after all job steps finish: (\w+)")
#: a context value in the job's message: "k": "repository" followed by its "v"
CONTEXT = '"k":\\s*"{}",\\s*"v":\\s*"([^"]*)"'
JOB_NAME = re.compile(r'"jobDisplayName":\s*"([^"]*)"')
#: how much of a log's start is read for the job's identity, and of its end for the result
HEAD_BYTES = 512 * 1024
TAIL_BYTES = 64 * 1024
#: a step must run this many times its time in the green run, and this many seconds more, to count. A test
#: step on a loaded machine that ran 2.2 to 2.5 times its green time until its job was cancelled is the case
#: these are set for, and a short step that ran four times longer but only a few minutes more is left out.
FACTOR = 2.0
MARGIN_S = 600.0
#: a log with no job result, not written for this long, has no live worker (a live one writes every ~10 s)
SILENT_S = 120.0


@dataclass
class Step:
    name: str
    started: dt.datetime
    finished: dt.datetime | None = None
    result: str | None = None          # succeeded, failed, canceled, or None while it runs

    def seconds(self, now: dt.datetime | None = None) -> float | None:
        """How long it ran, or has run so far until `now`; None for a running step with no `now`."""
        end = self.finished or now
        return None if end is None else max(0.0, (end - self.started).total_seconds())


@dataclass
class Job:
    path: Path
    repository: str | None = None
    workflow: str | None = None
    name: str | None = None
    run_id: str | None = None
    attempt: str | None = None
    result: str | None = None          # Succeeded, Failed, Canceled, or None when the log has none yet
    steps: list[Step] = field(default_factory=list)
    written: dt.datetime | None = None   # when the log was last written

    def until(self, now: dt.datetime | None) -> dt.datetime | None:
        """Where an open step's time ends: `now` while a worker may still write the log, else its last write."""
        if now is None or self.written is None or (now - self.written).total_seconds() <= SILENT_S:
            return now
        return min(now, self.written)

    @property
    def key(self) -> tuple:
        """What makes two logs runs of the same job."""
        return (self.repository, self.workflow, self.name)


def _when(stamp: str) -> dt.datetime:
    return dt.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)


def _iso(text: str | None) -> dt.datetime | None:
    if not text:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?Z?$", text)
    if not m:
        return None
    return dt.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)


def identity(text: str) -> dict[str, str | None]:
    """The job's repository, workflow, name, run id and attempt, from its message in the log."""
    def ctx(k):
        m = re.search(CONTEXT.format(k), text)
        return m.group(1) if m else None
    m = JOB_NAME.search(text)
    return {"repository": ctx("repository"), "workflow": ctx("workflow"), "name": m.group(1) if m else None,
            "run_id": ctx("run_id"), "attempt": ctx("run_attempt")}


def parse(text: str, path: Path | str = "") -> Job:
    """A Worker log's text as a Job. A step skipped by its condition is left out, and telemetry that
    belongs to no processed step (the job's own set-up and end) is not a step."""
    job = Job(Path(path), **identity(text))
    open_step: Step | None = None
    block: list[str] | None = None
    for raw in text.splitlines():
        if block is not None:
            block.append(raw)
            if raw.rstrip() == "}.":
                try:
                    data = json.loads("\n".join(block).rstrip()[:-1])
                except ValueError:
                    data = {}
                block = None
                if open_step is not None and isinstance(data, dict):
                    open_step.started = _iso(data.get("startTime")) or open_step.started
                    open_step.finished = _iso(data.get("finishTime")) or open_step.finished
                    open_step.result = data.get("result") or open_step.result
                    open_step = None
            continue
        m = LINE.match(raw)
        if not m:
            continue
        at, words = _when(m.group(1)), m.group(4)
        if (p := PROCESSING.search(words)):
            open_step = Step(p.group(1), at)
        elif words.startswith("Skipping step") and open_step is not None:
            open_step = None
        elif words.startswith("Starting the step.") and open_step is not None:
            open_step.started = at
            job.steps.append(open_step)
        elif words.startswith(TELEMETRY):
            block = ["{"]
        elif (r := JOB_RESULT.search(words)):
            job.result = r.group(1)
    return job


def read(path: Path | str) -> Job | None:
    """The Worker log at `path` as a Job, or None when it cannot be read."""
    try:
        job = parse(Path(path).read_text(errors="replace"), path)
        job.written = dt.datetime.fromtimestamp(Path(path).stat().st_mtime, dt.timezone.utc)
    except OSError:
        return None
    return job


def _head(path: Path) -> str:
    with open(path, "rb") as fh:
        return fh.read(HEAD_BYTES).decode(errors="replace")


def _tail(path: Path) -> str:
    with open(path, "rb") as fh:
        size = fh.seek(0, 2)
        fh.seek(max(0, size - TAIL_BYTES))
        return fh.read().decode(errors="replace")


def logs(folder: Path | str, *, since: dt.datetime | None = None) -> list[Path]:
    """The folder's Worker logs, the most recently written first, only those written since `since` when
    given. Taken by when they were written, since a name says only when the worker started."""
    found = []
    for p in Path(folder).glob("Worker_*.log"):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if since is None or mtime >= since.timestamp():
            found.append((mtime, p))
    return [p for _, p in sorted(found, key=lambda t: t[0], reverse=True)]


def last_green(folder: Path | str, job: Job, *, limit: int = 60) -> Job | None:
    """The newest run of the same job (same repository, workflow and job name) that succeeded, among the
    folder's Worker logs older than `job`'s own, looking at no more than `limit` of them. Only the start
    and end of each log are read until one matches."""
    try:
        before = job.path.stat().st_mtime
    except OSError:
        before = None
    looked = 0
    for p in logs(folder):
        if p == job.path:
            continue
        try:
            if before is not None and p.stat().st_mtime >= before:
                continue
        except OSError:
            continue
        looked += 1
        if looked > limit:
            return None
        try:
            ident = identity(_head(p))
            ended = JOB_RESULT.search(_tail(p))
        except OSError:
            continue
        if (ident["repository"], ident["workflow"], ident["name"]) == job.key and ended \
                and ended.group(1) == "Succeeded":
            return read(p)
    return None


def slower(job: Job, green: Job | None, *, now: dt.datetime | None = None, factor: float = FACTOR,
           margin_s: float = MARGIN_S) -> list[dict]:
    """The steps of `job` that ran (or have run so far, until `now`) more than `factor` times the same
    step in `green` and at least `margin_s` seconds more: [{step, seconds, green_seconds, result}]. A
    step is matched by its name and how many steps of that name came before it, and one the green run
    did not run is not compared."""
    if green is None:
        return []
    seen: dict[str, int] = {}
    base: dict[tuple[str, int], float] = {}
    for s in green.steps:
        n = seen[s.name] = seen.get(s.name, -1) + 1
        took = s.seconds()
        if took is not None:
            base[(s.name, n)] = took
    seen.clear()
    out = []
    end = job.until(now)
    for s in job.steps:
        n = seen[s.name] = seen.get(s.name, -1) + 1
        was, took = base.get((s.name, n)), s.seconds(end)
        if was is None or took is None:
            continue
        if took > was * factor and took - was >= margin_s:
            out.append({"step": s.name, "seconds": round(took), "green_seconds": round(was), "result": s.result})
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="claude-ops runner-diag",
                                description="steps of a self-hosted runner's recent jobs that ran far longer than "
                                            "in the job's last successful run")
    p.add_argument("folder", help="the runner's _diag folder")
    p.add_argument("--hours", type=float, default=6, help="Worker logs written in this many last hours")
    p.add_argument("--factor", type=float, default=FACTOR, help="times the green run's time")
    p.add_argument("--margin", type=float, default=MARGIN_S, help="and at least this many seconds more")
    a = p.parse_args(argv)
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for path in logs(a.folder, since=now - dt.timedelta(hours=a.hours)):
        job = read(path)
        if job is None or not job.steps:
            continue
        green = last_green(a.folder, job)
        out.append({"log": path.name, "repository": job.repository, "workflow": job.workflow, "job": job.name,
                    "run_id": job.run_id, "attempt": job.attempt, "result": job.result,
                    "green_log": green.path.name if green else None,
                    "slower": slower(job, green, now=now, factor=a.factor, margin_s=a.margin)})
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

import json
import os

from claude_ops import cli, update
from claude_ops.watch import STALL_EXIT, stall, stall_check, turn_state

SID = "0d0e0f10-1111-4222-8333-444455556666"
START = 1_800_000_000.0


class Clock:
    """A clock the watch reads and a sleep that moves it, so no test waits."""

    def __init__(self, t=START, on_sleep=None):
        self.t, self.on_sleep, self.sleeps = t, on_sleep, 0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s
        self.sleeps += 1
        if self.on_sleep:
            self.on_sleep(self)


def user(text, n=1):
    return {"type": "user", "uuid": f"0d0e0f10-1111-4222-8333-00000000000{n}", "sessionId": SID,
            "timestamp": "2026-01-02T10:00:00Z", "message": {"role": "user", "content": text}}


def assistant(content, stop=None, n=2, **extra):
    return {"type": "assistant", "uuid": f"0d0e0f10-1111-4222-8333-00000000001{n}", "sessionId": SID,
            "timestamp": "2026-01-02T10:00:05Z", **extra,
            "message": {"role": "assistant", "stop_reason": stop, "content": content}}


TOOL_CALL = assistant([{"type": "tool_use", "id": "toolu_01", "name": "Bash", "input": {"command": "ls"}}],
                      stop="tool_use")
TOOL_RESULT = {"type": "user", "uuid": "0d0e0f10-1111-4222-8333-000000000021", "sessionId": SID,
               "timestamp": "2026-01-02T10:00:07Z",
               "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_01",
                                                        "content": "a.txt"}]}}
ENDED = assistant([{"type": "text", "text": "All done."}], stop="end_turn", n=3)
BOOKKEEPING = [{"type": "system", "subtype": "turn_duration", "durationMs": 5000},
               {"type": "last-prompt", "lastPrompt": "list the files"}]


def write(path, records, age_s=0, clock_t=START):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    os.utime(path, (clock_t - age_s, clock_t - age_s))
    return path


def test_a_transcript_that_ended_its_turn_is_finished(tmp_path):
    t = write(tmp_path / f"{SID}.jsonl", [user("list the files"), TOOL_CALL, TOOL_RESULT, ENDED], age_s=7200)
    clock = Clock()
    r = stall(t, minutes=30, now=clock.now, sleep=clock.sleep)
    assert r["result"] == "finished" and "ended its turn" in r["line"]
    assert clock.sleeps == 0 and STALL_EXIT["finished"] == 0


def test_bookkeeping_after_the_end_of_turn_still_reads_as_finished(tmp_path):
    t = write(tmp_path / "agent-a1.jsonl", [user("go"), ENDED, *BOOKKEEPING])
    assert turn_state(t) == "ended"
    assert update.last_message(t)["uuid"] == ENDED["uuid"]
    assert stall_check(t, now=START)["result"] == "finished"


def test_an_old_transcript_stopped_mid_tool_call_is_stalled(tmp_path):
    t = write(tmp_path / "agent-a2.jsonl", [user("go"), TOOL_CALL], age_s=40 * 60)
    clock = Clock()
    r = stall(t, minutes=30, now=clock.now, sleep=clock.sleep)
    assert r["result"] == "stalled" and clock.sleeps == 0
    assert "unchanged for 40 minutes" in r["line"] and "a tool call with no result" in r["line"]
    assert STALL_EXIT["stalled"] == 1


def test_an_old_transcript_waiting_after_a_tool_result_is_stalled(tmp_path):
    t = write(tmp_path / "agent-a3.jsonl", [user("go"), TOOL_CALL, TOOL_RESULT], age_s=31 * 60)
    r = stall_check(t, minutes=30, now=START)
    assert r["result"] == "stalled" and "a tool result with no answer" in r["line"]


def test_a_quiet_transcript_is_stalled_once_the_limit_passes(tmp_path):
    t = write(tmp_path / "agent-a4.jsonl", [user("go"), TOOL_CALL])
    clock = Clock()
    r = stall(t, minutes=30, poll_s=60, now=clock.now, sleep=clock.sleep)
    assert r["result"] == "stalled" and clock.t - START == 30 * 60


def test_a_transcript_that_keeps_changing_is_never_stalled_and_times_out(tmp_path):
    t = write(tmp_path / "agent-a5.jsonl", [user("go"), TOOL_CALL])

    def work(clock):  # the session writes a record on every minute
        with open(t, "a") as f:
            f.write(json.dumps(TOOL_RESULT) + "\n")
        os.utime(t, (clock.t, clock.t))

    clock = Clock(on_sleep=work)
    r = stall(t, minutes=30, max_hours=2, poll_s=60, now=clock.now, sleep=clock.sleep)
    assert r["result"] == "timeout" and "still changing after 2 hours" in r["line"]
    assert clock.t - START == 2 * 3600 and STALL_EXIT["timeout"] == 2


def test_an_api_error_ends_the_wait_at_once(tmp_path):
    err = assistant([{"type": "text", "text": "API Error: the usage limit was reached"}], stop="stop_sequence",
                    n=4, isApiErrorMessage=True)
    t = write(tmp_path / "agent-a6.jsonl", [user("go"), err])
    r = stall_check(t, now=START)
    assert r["result"] == "error" and "usage limit" in r["line"] and STALL_EXIT["error"] == 1


def test_a_folder_finishes_when_every_transcript_ended(tmp_path):
    folder = tmp_path / SID / "subagents"
    write(folder / "agent-b1.jsonl", [user("one"), ENDED])
    second = write(folder / "agent-b2.jsonl", [user("two"), TOOL_CALL])
    assert stall_check(folder, now=START) is None          # one is still working, and recent
    r = stall_check(folder, minutes=30, now=START + 45 * 60)
    assert r["result"] == "stalled" and "1 of 2 transcripts have not ended" in r["line"]
    write(second, [user("two"), TOOL_CALL, TOOL_RESULT, ENDED])
    r = stall_check(folder, now=START)
    assert r["result"] == "finished" and "all 2 transcripts" in r["line"]


def test_a_folder_is_moving_while_any_file_in_it_changes(tmp_path):
    folder = tmp_path / "out"
    write(folder / "agent-c1.jsonl", [user("go"), TOOL_CALL], age_s=3 * 3600)
    (folder / "deep").mkdir()
    fresh = folder / "deep" / "progress.log"
    fresh.write_text("step 4\n")
    os.utime(fresh, (START - 60, START - 60))
    assert stall_check(folder, minutes=30, now=START) is None


def test_the_done_file_ends_the_wait_only_when_written(tmp_path):
    t = write(tmp_path / "agent-d1.jsonl", [user("go"), TOOL_CALL])
    done = tmp_path / "result.txt"
    done.write_text("")

    def finish(clock):
        if clock.sleeps == 3:
            done.write_text("result\n")

    clock = Clock(on_sleep=finish)
    r = stall(t, minutes=30, done_file=done, now=clock.now, sleep=clock.sleep)
    assert r["result"] == "done" and clock.sleeps == 3 and str(done) in r["line"]


def test_a_missing_path_is_gone(tmp_path):
    clock = Clock()
    r = stall(tmp_path / "missing.jsonl", now=clock.now, sleep=clock.sleep)
    assert r["result"] == "gone" and clock.sleeps == 0 and STALL_EXIT["gone"] == 0


def test_the_command_prints_one_line_and_exits_by_result(tmp_path, capsys):
    finished = write(tmp_path / "e1.jsonl", [user("go"), ENDED])
    assert cli.main(["stall", str(finished)]) == 0
    assert "finished:" in capsys.readouterr().out
    old = write(tmp_path / "e2.jsonl", [user("go"), TOOL_CALL])
    os.utime(old, (1, 1))
    assert cli.main(["stall", str(old), "--minutes", "5", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["result"] == "stalled"
    fresh = write(tmp_path / "e3.jsonl", [user("go"), TOOL_CALL])
    os.utime(fresh, None)
    assert cli.main(["stall", str(fresh), "--max-hours", "0"]) == 2
    assert "still changing" in capsys.readouterr().out

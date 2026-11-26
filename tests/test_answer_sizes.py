"""answer_sizes reads tool answer lengths from transcripts: by the answer's own date, without the MCP
wrapper, for the tools named by a prefix, and never keeps the text."""
import json
import os
import time

from claude_ops import answer_sizes as A

P = "mcp__shop__"


def use(i, name, ts="2026-11-20T10:00:00.000Z"):
    return json.dumps({"timestamp": ts, "message": {"content": [{"type": "tool_use", "id": i, "name": name}]}})


def result(i, text, ts="2026-11-20T10:00:01.000Z", as_list=True):
    content = [{"type": "text", "text": text}] if as_list else text
    return json.dumps({"timestamp": ts, "message": {"content": [{"type": "tool_result", "tool_use_id": i,
                                                                  "content": content}]}})


def test_an_mcp_wrapper_is_taken_off_before_the_length_is_counted():
    inner = '{\n  "rows": ["a \\"quoted\\" word"]\n}'
    wrapped = json.dumps({"result": inner})
    assert len(wrapped) > len(inner)
    assert A.answer_text([{"type": "text", "text": wrapped}]) == inner
    assert A.answer_text("plain words") == "plain words"
    assert A.answer_text('{"result": "x", "more": 1}') == '{"result": "x", "more": 1}', "only a lone result key is a wrapper"


def test_answers_are_named_by_their_tool_use_and_dated_by_their_own_line():
    lines = [use("a", P + "list-orders"), result("a", json.dumps({"result": "x" * 300})),
             use("b", P + "get_order", ts="2026-09-01T00:00:00.000Z"),
             result("b", "y" * 50, ts="2026-09-01T00:00:01.000Z"),
             use("c", "mcp__other__thing"), result("c", "z" * 999),
             use("d", P + "get_order"), result("d", "w" * 70, as_list=False)]
    got = A.sizes_in(lines, P, since="2026-11-01T00:00:00")
    assert got == [("list-orders", 300), ("get_order", 70)], got


def test_a_summary_gives_count_max_p99_and_how_many_were_over_the_limit():
    rows = A.summary([("a", 10), ("a", 30), ("b", 25000), ("b", 5)], limit=20000)
    assert rows[0] == {"tool": "b", "count": 2, "max": 25000, "p99": 25000, "over": 1, "at_max": 1, "cut": False}
    assert rows[1] == {"tool": "a", "count": 2, "max": 30, "p99": 30, "over": 0, "at_max": 1, "cut": False}


def test_several_answers_at_one_exact_largest_length_read_as_a_cut():
    rows = {r["tool"]: r for r in A.summary([("clipped", 20000), ("clipped", 20000), ("clipped", 20000),
                                             ("clipped", 512), ("real", 19980), ("real", 19000)])}
    assert rows["clipped"]["at_max"] == 3 and rows["clipped"]["cut"] is True
    assert rows["real"]["at_max"] == 1 and rows["real"]["cut"] is False


def test_measure_skips_a_file_older_than_the_window_and_reads_subagents(tmp_path):
    now = time.time()
    fresh = tmp_path / "proj" / "s1.jsonl"
    sub = tmp_path / "proj" / "s1" / "subagents" / "agent-1.jsonl"
    old = tmp_path / "proj" / "s0.jsonl"
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - 3600))
    for path, n in ((fresh, 40), (sub, 60), (old, 9000)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(use("u", P + "list") + "\n" + result("u", "q" * n, ts=stamp) + "\n")
    os.utime(old, (now - 40 * 86400,) * 2)
    out = A.measure(P, days=30, root=tmp_path, now=now)
    assert out["files"] == 2 and out["tools"] == [{"tool": "list", "count": 2, "max": 60, "p99": 60, "over": 0,
                                                    "at_max": 1, "cut": False}]
    assert "q" not in json.dumps(out), "no answer text is kept"


def test_the_command_prints_one_line_a_tool(tmp_path, capsys):
    f = tmp_path / "p" / "s.jsonl"
    f.parent.mkdir()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    f.write_text("\n".join([use("u", P + "list"), result("u", "q" * 21000, ts=stamp),
                             use("v", P + "cut"), result("v", "c" * 300, ts=stamp),
                             use("w", P + "cut"), result("w", "c" * 300, ts=stamp)]) + "\n")
    assert A.main(["--prefix", P, "--root", str(tmp_path), "--days", "1"]) == 0
    out = capsys.readouterr().out
    assert "list" in out and "max   21000" in out and "over 1" in out
    assert "2 answers end at exactly 300, which is how a cut looks" in out
    assert out.count("how a cut looks") == 1

import json

from claude_ops import tracebacks

ONE = """starting
Traceback (most recent call last):
  File "/srv/app/handlers.py", line 10, in render
    body = page[key]
           ~~~~^^^^^
KeyError: 'title'
served
"""

CHAINED = """Traceback (most recent call last):
  File "/srv/app/db.py", line 4, in read
    rows = run(q)
ValueError: bad row

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "/srv/app/api.py", line 20, in get
    return read()
  File "/usr/lib/python3/json/__init__.py", line 99, in dumps
    raise TypeError("no")
TypeError: no
"""

PIPE = """----------------------------------------
Exception occurred during processing of request from ('127.0.0.1', 5000)
Traceback (most recent call last):
  File "/usr/lib/python3/socketserver.py", line 800, in write
    self._sock.sendall(b)
BrokenPipeError: [Errno 32] Broken pipe
----------------------------------------
"""


def test_one_traceback_with_its_frame_and_exception():
    traces, incomplete = tracebacks.parse(ONE)
    assert incomplete is None and len(traces) == 1
    t = traces[0]
    assert t.offset == len("starting\n")
    assert t.frames == [("/srv/app/handlers.py", 10, "render")]
    assert (t.exc_type, t.message) == ("KeyError", "'title'")
    assert t.signature() == "KeyError at handlers.py:render"
    assert t.text.startswith("Traceback") and t.text.rstrip().endswith("KeyError: 'title'")


def test_a_chain_is_one_traceback_named_by_its_last_exception():
    traces, _ = tracebacks.parse(CHAINED)
    assert len(traces) == 1
    t = traces[0]
    assert t.exc_type == "TypeError" and t.chain == ["ValueError: bad row"]
    assert t.signature() == "TypeError at __init__.py:dumps"
    assert t.signature(tracebacks.globs(["/srv/app/*"])) == "TypeError at api.py:get"


def test_an_exception_without_a_message_and_dotted_types():
    traces, _ = tracebacks.parse(PIPE + "Traceback (most recent call last):\n  File \"/a.py\", line 1, in f\n"
                                 "    g()\npsycopg.errors.UndefinedTable: relation \"x\" does not exist\n"
                                 "Traceback (most recent call last):\n  File \"/a.py\", line 2, in h\n    k()\n"
                                 "StopIteration\n")
    assert [t.exc_type for t in traces] == ["BrokenPipeError", "psycopg.errors.UndefinedTable",
                                            "StopIteration"]
    assert traces[2].message == ""


def test_a_traceback_still_being_written_is_left_for_the_next_pass(tmp_path):
    log = tmp_path / "app.log"
    half = ONE.split("KeyError")[0]
    log.write_text(half)
    traces, nxt, skipped = tracebacks.read_new(log)
    assert traces == [] and nxt == len("starting\n") and skipped == 0
    log.write_text(ONE)
    traces, nxt, _ = tracebacks.read_new(log, nxt)
    assert [t.exc_type for t in traces] == ["KeyError"] and nxt == len(ONE)
    traces, again, _ = tracebacks.read_new(log, nxt)
    assert traces == [] and again == nxt


def test_a_chain_cut_after_its_first_exception_waits_too(tmp_path):
    log = tmp_path / "app.log"
    cut = CHAINED.split("Traceback (most recent call last):\n  File \"/srv/app/api.py\"")[0]
    log.write_text(cut)
    traces, nxt, _ = tracebacks.read_new(log)
    assert traces == [] and nxt == 0


def test_a_partial_last_line_is_not_read(tmp_path):
    log = tmp_path / "app.log"
    log.write_text(ONE + "Traceback (most rec")
    traces, nxt, _ = tracebacks.read_new(log)
    assert len(traces) == 1 and nxt == len(ONE)


def test_a_rotated_log_is_read_from_the_start(tmp_path):
    log = tmp_path / "app.log"
    log.write_text(ONE * 3)
    _, nxt, _ = tracebacks.read_new(log)
    log.write_text(ONE)
    traces, again, _ = tracebacks.read_new(log, nxt)
    assert len(traces) == 1 and again == len(ONE)


def test_a_backlog_over_the_cap_is_skipped_from_a_line_start(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("x" * 50 + "\n" + ONE)
    traces, nxt, skipped = tracebacks.read_new(log, 0, max_bytes=len(ONE) + 10)
    assert skipped == 51 and len(traces) == 1 and nxt == 51 + len(ONE)


def test_group_counts_by_signature_and_keeps_the_newest():
    traces, _ = tracebacks.parse(ONE + ONE.replace("'title'", "'body'") + PIPE)
    g = tracebacks.group(traces)
    assert [(x["signature"], x["count"]) for x in g] == [("KeyError at handlers.py:render", 2),
                                                         ("BrokenPipeError at socketserver.py:write", 1)]
    assert g[0]["message"] == "'body'" and g[0]["frame"]["line"] == 10 and g[0]["first"] < g[0]["last"]


def test_offsets_count_bytes_not_characters(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("préface ✓\n" + ONE, encoding="utf-8")
    traces, nxt, _ = tracebacks.read_new(log)
    assert traces[0].offset == len("préface ✓\nstarting\n".encode()) and nxt == log.stat().st_size


def test_cli_json_and_ignore(tmp_path, capsys):
    log = tmp_path / "app.log"
    log.write_text(ONE + PIPE)
    assert tracebacks.main([str(log), "--json", "--ignore", "BrokenPipeError"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["tracebacks"] == 1 and out["groups"][0]["signature"] == "KeyError at handlers.py:render"
    assert tracebacks.main([str(log)]) == 0
    assert "2 traceback(s) in 2 group(s)" in capsys.readouterr().out


def test_two_threads_writing_at_once_keep_what_was_complete():
    first = CHAINED.split("Traceback (most recent call last):\n  File \"/srv/app/api.py\"")[0]
    traces, _ = tracebacks.parse(first + "Traceback (most recent call last):\n" + ONE.split("\n", 1)[1]
                                 + "Traceback (most recent call last):\nTraceback (most recent call last):\n"
                                 + PIPE.split("Exception occurred during processing of request from ('127.0.0.1', 5000)\n")[1])
    assert [t.exc_type for t in traces] == ["ValueError", "KeyError", "BrokenPipeError"]
    assert traces[0].chain == [] and traces[0].frames == [("/srv/app/db.py", 4, "read")]

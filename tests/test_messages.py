import json

from claude_ops import messages

from conftest import write_chat


def _u(ts, text, **kw):
    return {"type": "user", "timestamp": ts, "sessionId": "s1", "message": {"content": text}, **kw}


def _a(ts, text):
    return {"type": "assistant", "timestamp": ts, "sessionId": "s1",
            "message": {"content": [{"type": "text", "text": text}]}}


def _tool(ts, name, desc):
    return {"type": "assistant", "timestamp": ts, "sessionId": "s1",
            "message": {"content": [{"type": "tool_use", "name": name, "input": {"description": desc}}]}}


def _q(ts, text):
    return {"type": "queue-operation", "operation": "enqueue", "timestamp": ts, "sessionId": "s1",
            "content": text}


def _att(ts, text):
    return {"type": "attachment", "timestamp": ts, "sessionId": "s1",
            "attachment": {"type": "queued_command", "prompt": text, "commandMode": "prompt"}}


def _chat(tmp_path, records, title="build"):
    root = tmp_path / "projects"
    p = write_chat(root, "-work-app", "s1", [{"customTitle": title}, *records])
    return root, p


def test_classify_tells_the_person_from_programs_and_the_system():
    assert messages.classify("fix the login page") == ("person", "fix the login page")
    assert messages.classify("<task-notification>done</task-notification>")[0] == "system"
    assert messages.classify("Stop hook feedback: open work")[0] == "system"
    assert messages.classify("[watcher] the build is green")[0] == "program"
    assert messages.classify("[Image: original 100x100]")[0] == "system"
    assert messages.classify("<command-name>/model</command-name><command-args>opus</command-args>") \
        == ("command", "/model opus")
    # a paste a program made (its text opens with a tag) is the program's
    tagged = '<pasted_content id="1">\n[watcher] note: a thing\n</pasted_content id="1">'
    assert messages.classify(tagged)[0] == "program"
    # a closing tag that is missing still counts
    assert messages.classify('<pasted_content id="1">\n[watcher] a thing')[0] == "program"
    # the person's own paste, with or without words around it
    assert messages.classify('<pasted_content id="2">a log line</pasted_content id="2">')[0] == "pasted"
    assert messages.classify('why this? <pasted_content id="2">a log</pasted_content>')[0] == "pasted"


def test_person_messages_keep_only_what_was_typed(tmp_path):
    root, p = _chat(tmp_path, [
        _u("2026-01-01T10:00:00Z", "fix the login page", origin={"kind": "human"}, promptSource="typed"),
        _a("2026-01-01T10:01:00Z", "on it"),
        _u("2026-01-01T10:02:00Z", "<task-notification>x</task-notification>", origin={"kind": "task-notification"}),
        _u("2026-01-01T10:03:00Z", "From another session", origin={"kind": "peer"}),
        _u("2026-01-01T10:04:00Z", "a skill body", isMeta=True),
        _u("2026-01-01T10:05:00Z", "This session is being continued", isCompactSummary=True),
        _u("2026-01-01T10:06:00Z", "[watcher] build green", origin={"kind": "human"}),
        _u("2026-01-01T10:07:00Z", "check the PR", scheduledTaskId="t1", turnOrigin="scheduled"),
        _u("2026-01-01T10:08:00Z", [{"type": "tool_result", "content": "ok"}]),
        _u("2026-01-01T10:09:00Z", "noop", entrypoint="sdk-cli"),
        _u("2026-01-01T10:10:00Z", [{"type": "image", "source": {}}, {"type": "text", "text": "this one"}]),
        _u("2026-01-01T10:11:00Z", "<command-name>/clear</command-name>"),
    ])
    got = messages.person_messages([p])
    assert [e["text"] for e in got] == ["fix the login page", "this one [1 image(s)]"]
    assert got[0]["id"] == f"s1:{2}" and got[0]["how"] == "typed"
    with_commands = messages.person_messages([p], commands=True)
    assert with_commands[-1]["text"] == "/clear" and with_commands[-1]["how"] == "command"


def test_a_queued_message_is_found_once_whichever_shape_holds_it(tmp_path):
    root, p = _chat(tmp_path, [
        _u("2026-01-01T10:00:00Z", "start", origin={"kind": "human"}),
        # typed while Claude worked, never delivered as a user record
        _q("2026-01-01T10:01:00Z", "also keep backups"),
        # typed while Claude worked, absorbed into the turn as an attachment
        _q("2026-01-01T10:02:00Z", "and use the new name"),
        _att("2026-01-01T10:02:30Z", "and use the new name"),
        # typed while Claude worked, delivered later as a queued user record
        _q("2026-01-01T10:03:00Z", "then deploy"),
        _u("2026-01-01T10:09:00Z", "then deploy", origin={"kind": "human"}, promptSource="queued"),
        # a schedule's prompt is queued too, and its delivered record says whose it is
        _q("2026-01-01T10:10:00Z", "check the PR"),
        _u("2026-01-01T10:11:00Z", "check the PR", scheduledTaskId="t1", turnOrigin="scheduled"),
        # the same words typed again later are a new message
        _u("2026-01-01T18:00:00Z", "then deploy", origin={"kind": "human"}, promptSource="typed"),
    ])
    got = messages.person_messages([p])
    assert [(e["text"], e["how"]) for e in got] == [
        ("start", "typed"), ("also keep backups", "queued"), ("and use the new name", "queued"),
        ("then deploy", "queued"), ("then deploy", "typed")]
    assert got[2]["copies"] == [5]


def test_range_and_grep(tmp_path):
    root, p = _chat(tmp_path, [
        _u("2026-01-01T10:00:00Z", "first ask"),
        _u("2026-01-02T10:00:00Z", "second ask about backups"),
        _u("2026-01-03T10:00:00Z", "third ask"),
    ])
    since = messages.parse_when("2026-01-02T00:00:00+00:00")
    until = messages.parse_when("2026-01-03T00:00:00+00:00")
    assert [e["text"] for e in messages.person_messages([p], since=since, until=until)] == ["second ask about backups"]
    assert [e["text"] for e in messages.person_messages([p], grep="BACKUP")] == ["second ask about backups"]
    assert messages.parse_when("2d", now=1_000_000) == 1_000_000 - 2 * 86400


def test_select_by_title_folder_and_session():
    idx = [{"id": "aaa1", "title": "build", "cwd": "/work/app", "active_at": 100},
           {"id": "bbb2", "title": "docs-site", "cwd": "/work/app/docs", "active_at": 100},
           {"id": "ccc3", "title": None, "cwd": "/work/other", "active_at": 10}]
    assert [e["id"] for e in messages.select(idx, chats=["BUILD"])] == ["aaa1"]
    assert [e["id"] for e in messages.select(idx, chats=["docs-*"])] == ["bbb2"]
    assert [e["id"] for e in messages.select(idx, folders=["/work/app"])] == ["aaa1", "bbb2"]
    assert [e["id"] for e in messages.select(idx, session_ids=["ccc"])] == ["ccc3"]
    assert [e["id"] for e in messages.select(idx, since=50)] == ["aaa1", "bbb2"]


def test_around_shows_the_conversation_and_marks_the_message(tmp_path):
    root, p = _chat(tmp_path, [
        _u("2026-01-01T10:00:00Z", "fix the login page"),
        _a("2026-01-01T10:01:00Z", "looking"),
        _tool("2026-01-01T10:02:00Z", "Bash", "run the tests"),
        _q("2026-01-01T10:03:00Z", "and the signup page"),
        _att("2026-01-01T10:03:10Z", "and the signup page"),
        _a("2026-01-01T10:04:00Z", "both fixed"),
        _u("2026-01-01T10:05:00Z", "thanks"),
    ])
    rows = messages.around(p, 5, before=1, after=1)
    assert [(e["role"], e["text"], e["here"]) for e in rows] == [
        ("assistant", "looking", False), ("person", "and the signup page", True),
        ("assistant", "both fixed", False)]
    # the line of a folded copy finds the message too
    assert [e["here"] for e in messages.around(p, 6, before=0, after=0)] == [True]
    with_tools = messages.around(p, 5, before=1, after=0, tools=True)
    assert with_tools[0]["text"] == "Bash: run the tests"
    path, line = messages.locate("s1:5", root)
    assert path == p and line == 5


def test_cli_lists_and_shows_context(tmp_path, monkeypatch, capsys):
    root, p = _chat(tmp_path, [
        _u("2026-01-01T10:00:00Z", "fix the login page"),
        _a("2026-01-01T10:01:00Z", "done"),
    ])
    monkeypatch.setattr(messages.sessions, "build_index",
                        lambda: [{"id": "s1", "path": str(p), "title": "build", "cwd": "/work/app",
                                  "active_at": 2e9}])
    monkeypatch.setattr(messages, "ROOT", root)
    assert messages.main(["--since", "2025-12-01", "--chat", "build", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(e["text"], e["title"]) for e in rows] == [("fix the login page", "build")]
    monkeypatch.setattr(messages, "locate", lambda mid: (p, 2))
    assert messages.main(["--around", "s1:2", "--after", "1"]) == 0
    out = capsys.readouterr().out
    assert ">>" in out and "fix the login page" in out and "done" in out

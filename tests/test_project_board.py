"""project_board: a project kept in step with outside records, writing only what changed."""
import json
import re
import subprocess
from datetime import datetime, timezone

import pytest

from claude_ops import project_board
from claude_ops.project_board import Board, BoardError, OutOfBudget, RateLimited


class AlreadyThere(Exception):
    pass


class LabelTaken(Exception):
    pass


def run(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


class FakeGitHub:
    """gh api graphql against an in-memory repository and project. Each call is kept with the
    operation it ran (the first word after `mutation` or the query's first field)."""

    def __init__(self, project=False, fail=None):
        self.calls, self.ops = [], []
        self.issues, self.items, self.fields = {}, {}, {}
        self.project = {"id": "P1", "number": 3, "title": "Work", "url": "https://github.com/users/o/projects/3"} \
            if project else None
        self.parents, self.fail, self.n, self.ignored = {}, fail, 0, False
        self.labels, self.labelled, self.views, self.label_race = {}, {}, [], False

    def _id(self, prefix):
        self.n += 1
        return f"{prefix}{self.n}"

    def __call__(self, args, stdin=None):
        self.calls.append(args)
        if args[:3] == ["api", "-X", "PUT"]:
            self.ignored = True
            return run(out="{}")
        req = json.loads(stdin)
        q, v = req["query"], req["variables"]
        if q.lstrip().startswith("mutation"):
            op = re.search(r"\)\s*\{\s*(\w+)", q).group(1)      # after the variable list
        else:
            op = re.search(r"\{\s*(\w+)", q).group(1)
        self.ops.append(op)
        if self.fail and op == self.fail:
            return run(1, json.dumps({"errors": [{"message": f"{op} refused"}]}))
        try:
            return run(out=json.dumps({"data": getattr(self, op)(v)}))
        except AlreadyThere:
            return run(1, json.dumps({"errors": [{"message": "Content already exists in this project"}]}))
        except LabelTaken:
            return run(1, json.dumps({"errors": [{"message": "Name has already been taken"}]}))

    # queries
    def repository(self, v):
        nodes = [self.project] if self.project else []
        label = {"id": self.labels[v["label"]]} if v.get("label") in self.labels else None
        return {"repository": {"id": "R1", "label": label, "owner": {"id": "U1", "projectsV2": {"nodes": nodes}}}}

    def node(self, v):
        if v.get("id") in self.issues:
            items = [{"id": i, "project": {"id": "P1"}} for i, c in self.items.items() if c == v["id"]]
            return {"node": {"projectItems": {"nodes": items}}}
        if "after" in v:
            nodes = [{"id": i, "content": self.issues.get(c, {})} for i, c in self.items.items()]
            return {"node": {"items": {"pageInfo": {"hasNextPage": False}, "nodes": nodes}}}
        return {"node": {"views": {"nodes": [
                    {**x, "fields": {"nodes": [{"name": n} for n in x["fields"]]}} for x in self.views]},
                         "fields": {"nodes": [
            {"id": fid, "name": f["name"], "dataType": f["type"],
             **({"options": [{"id": oid, "name": n} for n, oid in f["options"].items()]}
                if f["type"] == "SINGLE_SELECT" else {})} for fid, f in self.fields.items()]}}}

    def resource(self, v):
        return {"resource": {"id": "PR9"} if "/pull/" in v["url"] else None}

    # labels and views
    def createLabel(self, v):
        if self.label_race:
            self.label_race = False
            self.labels[v["name"]] = self._id("L")
            raise LabelTaken
        self.labels[v["name"]] = self._id("L")
        return {"createLabel": {"label": {"id": self.labels[v["name"]]}}}

    def addLabelsToLabelable(self, v):
        self.labelled.setdefault(v["node"], []).extend(v["labels"])
        return {"addLabelsToLabelable": {"clientMutationId": None}}

    def addSubIssue(self, v):
        self.parents[v["child"]] = v["parent"]
        return {"addSubIssue": {"issue": {"id": v["child"]}}}

    def createProjectV2View(self, v):
        vid = self._id("V")
        self.views.append({"id": vid, "name": v["name"], "layout": v["layout"], "filter": "", "fields": []})
        return {"createProjectV2View": {"projectV2View": {"id": vid}}}

    def updateProjectV2View(self, v):
        view = next(x for x in self.views if x["id"] == v["id"])
        names = {fid: f["name"] for fid, f in self.fields.items()}
        view.update(layout=v["layout"], filter=v.get("filter") or "")
        if v.get("config"):
            view["fields"] = [names[i] for i in v["config"]["visibleFieldIds"]]
        return {"updateProjectV2View": {"projectV2View": {"id": v["id"]}}}

    def deleteProjectV2View(self, v):
        if len(self.views) == 1:
            raise AssertionError("a project keeps at least one view")
        self.views = [x for x in self.views if x["id"] != v["id"]]
        return {"deleteProjectV2View": {"clientMutationId": None}}

    # mutations
    def createProjectV2(self, v):
        self.project = {"id": "P1", "number": 3, "title": v["title"], "url": "https://github.com/users/o/projects/3"}
        return {"createProjectV2": {"projectV2": self.project}}

    def createProjectV2Field(self, v):
        fid = self._id("F")
        self.fields[fid] = {"name": v["name"], "type": v["kind"],
                            "options": {o["name"]: self._id("O") for o in v.get("options") or []}}
        return {"createProjectV2Field": {"projectV2Field": {"id": fid}}}

    def updateProjectV2Field(self, v):
        f = self.fields[v["field"]]
        f["options"] = {o["name"]: o.get("id") or self._id("O") for o in v["options"]}
        return {"updateProjectV2Field": {"projectV2Field": {"id": v["field"]}}}

    def createIssue(self, v):
        nid = self._id("I")
        number = len(self.issues) + 1
        self.issues[nid] = {"title": v["title"], "body": v["body"], "closed": False, "closedAt": None,
                            "number": number, "values": {}}
        return {"createIssue": {"issue": {"id": nid, "number": number, "url": f"https://github.com/o/r/issues/{number}"}}}

    def updateIssue(self, v):
        self.issues[v["id"]].update(title=v["title"], body=v["body"])
        return {"updateIssue": {"issue": {"id": v["id"]}}}

    def closeIssue(self, v):
        self.issues[v["id"]].update(closed=True, closedAt="2026-01-01T00:00:00Z")
        return {"closeIssue": {"issue": {"id": v["id"]}}}

    def reopenIssue(self, v):
        self.issues[v["id"]].update(closed=False, closedAt=None)
        return {"reopenIssue": {"issue": {"id": v["id"]}}}

    def addSubIssue(self, v):
        self.parents[v["child"]] = v["parent"]
        return {"addSubIssue": {"issue": {"id": v["parent"]}}}

    def removeSubIssue(self, v):
        self.parents.pop(v["child"], None)
        return {"removeSubIssue": {"issue": {"id": v["parent"]}}}

    def addProjectV2ItemById(self, v):
        for i, c in self.items.items():
            if c == v["content"]:
                raise AlreadyThere(i)
        iid = self._id("T")
        self.items[iid] = v["content"]
        return {"addProjectV2ItemById": {"item": {"id": iid}}}

    def updateProjectV2ItemFieldValue(self, v):
        self.issues.get(self.items[v["item"]], {}).setdefault("values", {})[v["field"]] = v["value"]
        return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": v["item"]}}}

    def clearProjectV2ItemFieldValue(self, v):
        self.issues.get(self.items[v["item"]], {}).get("values", {}).pop(v["field"], None)
        return {"clearProjectV2ItemFieldValue": {"projectV2Item": {"id": v["item"]}}}

    def updateProjectV2(self, v):
        self.project.update({k: v[k] for k in ("public", "shortDescription") if k in v})
        return {"updateProjectV2": {"projectV2": {"id": v["id"]}}}

    def deleteProjectV2Item(self, v):
        self.items.pop(v["item"])
        return {"deleteProjectV2Item": {"deletedItemId": v["item"]}}


def board(gh, **kw):
    kw.setdefault("selects", {"State": ["Open", "Done"], "Owner": ["alpha"]})
    kw.setdefault("texts", ("Topic",))
    kw.setdefault("pause", 0)
    return Board("o/r", "Work", gh=gh, **kw)


def test_ensure_makes_the_project_and_its_fields_once():
    gh = FakeGitHub()
    b = board(gh)
    assert b.ensure()["id"] == "P1"
    assert sorted(f["name"] for f in gh.fields.values()) == ["Owner", "State", "Topic"]
    assert gh.ops.count("createProjectV2") == 1
    again = board(gh)
    again.ensure()
    assert gh.ops.count("createProjectV2") == 1 and gh.ops.count("createProjectV2Field") == 3


def test_ensure_uses_an_existing_project_and_field():
    gh = FakeGitHub(project=True)
    gh.fields["F0"] = {"name": "State", "type": "SINGLE_SELECT", "options": {"Open": "O0"}}
    b = board(gh)
    b.ensure()
    assert "createProjectV2" not in gh.ops
    assert gh.ops.count("createProjectV2Field") == 2       # Owner and Topic only


def test_put_creates_the_issue_with_its_marker_item_and_fields():
    gh = FakeGitHub()
    b = board(gh)
    b.ensure()
    had = b.put("k1", title="Do it", body="the text", fields={"State": "Open", "Topic": "backups"})
    issue = gh.issues[had["node"]]
    assert issue["body"].startswith("<!-- board-key: k1 -->\n") and issue["body"].endswith("the text")
    assert had["number"] == 1 and had["item"] in gh.items
    assert had["fields"] == {"State": "Open", "Topic": "backups"}
    assert {"text": "backups"} in issue["values"].values()


def test_put_with_nothing_changed_writes_nothing():
    gh = FakeGitHub()
    b = board(gh)
    b.ensure()
    had = b.put("k1", title="Do it", body="x", fields={"State": "Open"})
    before = b.writes
    again = b.put("k1", title="Do it", body="x", fields={"State": "Open"}, had=had)
    assert b.writes == before and again == had


def test_put_writes_only_the_changes():
    gh = FakeGitHub()
    b = board(gh)
    b.ensure()
    had = b.put("k1", title="Do it", body="x", fields={"State": "Open", "Topic": "a"})
    gh.ops.clear()
    had = b.put("k1", title="Do it now", body="x", closed=True, fields={"State": "Done", "Topic": "a"}, had=had)
    assert gh.ops == ["updateIssue", "closeIssue", "updateProjectV2ItemFieldValue"]
    assert gh.issues[had["node"]]["closed"] and had["closed"]
    gh.ops.clear()
    b.put("k1", title="Do it now", body="x", closed=False, fields={"State": "Done", "Topic": "a"}, had=had)
    assert gh.ops == ["reopenIssue"]


def test_a_new_select_value_is_added_keeping_the_old_options():
    gh = FakeGitHub()
    b = board(gh)
    b.ensure()
    old = dict(b.fields["Owner"]["options"])
    b.put("k1", title="t", body="b", fields={"Owner": "beta"})
    owner = next(f for f in gh.fields.values() if f["name"] == "Owner")
    assert owner["options"]["alpha"] == old["alpha"] and "beta" in owner["options"]


def test_an_empty_value_clears_the_field():
    gh = FakeGitHub()
    b = board(gh)
    b.ensure()
    had = b.put("k1", title="t", body="b", fields={"Topic": "a"})
    gh.ops.clear()
    b.put("k1", title="t", body="b", fields={"Topic": ""}, had=had)
    assert gh.ops == ["clearProjectV2ItemFieldValue"]


def test_parent_is_set_moved_and_removed():
    gh = FakeGitHub()
    b = board(gh)
    b.ensure()
    p1 = b.put("goal-1", title="Goal", body="")
    p2 = b.put("goal-2", title="Goal two", body="")
    child = b.put("k1", title="t", body="b", parent=p1["node"])
    assert gh.parents[child["node"]] == p1["node"]
    child = b.put("k1", title="t", body="b", parent=p2["node"], had=child)
    assert gh.parents[child["node"]] == p2["node"]
    child = b.put("k1", title="t", body="b", parent=None, had=child)
    assert child["node"] not in gh.parents


def test_budget_stops_before_a_record_it_cannot_finish():
    gh = FakeGitHub()
    b = board(gh, budget=project_board.PUT_WRITES + 3)
    b.ensure()                                     # project and three fields: four writes
    with pytest.raises(OutOfBudget):
        b.put("k1", title="t", body="b")
    assert not gh.issues


def test_a_failure_part_way_keeps_what_was_written():
    gh = FakeGitHub(fail="addProjectV2ItemById")
    b = board(gh)
    b.ensure()
    with pytest.raises(BoardError) as e:
        b.put("k1", title="t", body="b")
    assert e.value.had["node"] in gh.issues and not e.value.had.get("item")
    gh.fail = None
    gh.ops.clear()
    b.put("k1", title="t", body="b", had=e.value.had)
    assert "createIssue" not in gh.ops and gh.ops == ["addProjectV2ItemById"]


def test_rate_limit_is_its_own_error():
    def gh(args, stdin=None):
        return run(1, json.dumps({"errors": [{"message": "You have exceeded a secondary rate limit"}]}))
    with pytest.raises(RateLimited):
        board(gh).ensure()


REFUSED = ('Post "https://api.github.com/graphql": http2: Transport: cannot retry err [stream error: stream ID 1; '
           'REFUSED_STREAM; received from peer] after Request.Body was written; define Request.GetBody to avoid this error')


def test_a_refused_stream_is_sent_again():
    gh, waits, refusals = FakeGitHub(), [], [1]

    def flaky(args, stdin=None):
        if refusals:
            refusals.pop()
            return run(1, "", REFUSED)
        return gh(args, stdin)
    b = board(flaky, sleep=waits.append)
    project = b.ensure()
    # one wait for the refusal (the rest are the writes' own pause, 0 here)
    assert project["number"] and [w for w in waits if w] == [project_board.REFUSED_WAIT]
    assert gh.ops[0] == "repository"          # the refused query reached GitHub once, the second time


def test_a_stream_refused_every_time_is_an_error_after_the_last_try():
    calls, waits = [], []

    def gh(args, stdin=None):
        calls.append(args)
        return run(1, "", REFUSED)
    with pytest.raises(BoardError, match="REFUSED_STREAM"):
        board(gh, sleep=waits.append).ensure()
    assert len(calls) == project_board.REFUSED_TRIES
    assert waits == [project_board.REFUSED_WAIT * n for n in range(1, project_board.REFUSED_TRIES)]


def test_any_other_failure_is_not_sent_again():
    calls = []

    def gh(args, stdin=None):
        calls.append(args)
        return run(1, "", "HTTP 502: Bad Gateway")
    with pytest.raises(BoardError, match="502"):
        board(gh, sleep=lambda s: None).ensure()
    assert len(calls) == 1


def test_writes_wait_between_each_other():
    gh, waits = FakeGitHub(), []
    b = board(gh, pause=1.5, sleep=waits.append)
    b.ensure()
    assert waits == [1.5] * (b.writes - 1)


def test_prune_takes_off_only_items_closed_long_enough():
    gh = FakeGitHub()
    b = board(gh)
    b.ensure()
    old = b.put("old", title="t", body="b", closed=True)
    new = b.put("new", title="t", body="b", closed=True)
    gh.issues[new["node"]]["closedAt"] = "2026-03-10T00:00:00Z"
    still = b.put("open", title="t", body="b")
    removed = b.prune(days=14, now=datetime(2026, 3, 12, tzinfo=timezone.utc))
    assert removed == 1
    assert old["item"] not in gh.items and new["item"] in gh.items and still["item"] in gh.items
    assert old["node"] in gh.issues                # the issue itself stays


def test_pull_request_by_url():
    gh = FakeGitHub()
    b = board(gh)
    b.ensure()
    assert b.add_pull_request("https://github.com/o/r/pull/4") in gh.items
    assert b.add_pull_request("https://github.com/o/r/issues/4") is None


def test_ignore_notifications():
    gh = FakeGitHub()
    board(gh).ignore_notifications()
    assert gh.ignored and gh.calls[-1][-2:] == ["-F", "ignored=true"]


def test_an_issue_a_workflow_already_added_is_found_not_added_twice():
    gh = FakeGitHub()
    b = board(gh)
    b.ensure()
    parent = b.put("goal", title="Goal", body="")
    # the project's workflow adds every new sub-issue of an issue it holds
    original = gh.addSubIssue

    def add_and_auto_add(v):
        out = original(v)
        gh.items["T-auto"] = v["child"]
        return out
    gh.addSubIssue = add_and_auto_add
    child = b.put("k1", title="t", body="b", parent=parent["node"], fields={"State": "Open"})
    assert child["item"] == "T-auto"
    assert list(gh.items.values()).count(child["node"]) == 1


def test_visibility_and_description_are_set_only_when_they_differ():
    gh = FakeGitHub()
    b = board(gh, public=True, description="Issues of the tools")
    b.ensure()
    assert gh.project["public"] is True and gh.project["shortDescription"] == "Issues of the tools"
    assert gh.ops.count("updateProjectV2") == 1
    board(gh, public=True, description="Issues of the tools").ensure()
    assert gh.ops.count("updateProjectV2") == 1          # already so: no write
    board(gh).ensure()
    assert gh.ops.count("updateProjectV2") == 1          # None leaves both as they are
    board(gh, public=False).ensure()
    assert gh.project["public"] is False and gh.ops.count("updateProjectV2") == 2


def board_with_fields(gh):
    b = Board("o/r", "Work", selects={"State": ["Open"]}, texts=("Topic",), gh=gh, sleep=lambda _: None)
    b.ensure()
    return b


def test_a_missing_label_is_made_and_one_that_exists_is_found():
    gh = FakeGitHub(project=True)
    b = board_with_fields(gh)
    gh.labels["kind:bug"] = "L-old"
    b.set_labels("I1", ["kind:bug", "area:hooks"], repo="o/other")
    assert gh.ops.count("createLabel") == 1 and "area:hooks" in gh.labels
    assert gh.labelled["I1"] == ["L-old", gh.labels["area:hooks"]]
    made = gh.ops.count("repository")
    b.set_labels("I2", ["kind:bug"], repo="o/other")
    assert gh.ops.count("repository") == made, "a label read once is not read again"


def test_a_label_another_writer_made_first_is_used():
    gh = FakeGitHub(project=True)
    b = board_with_fields(gh)
    gh.label_race = True
    assert b.label_id("kind:ci") == gh.labels["kind:ci"]


def test_put_adds_only_the_labels_had_does_not_list_and_takes_none_off():
    gh = FakeGitHub()
    b = board_with_fields(gh)
    had = b.put("k", title="T", body="B", labels=["kind:bug"])
    assert had["labels"] == ["kind:bug"] and gh.ops.count("addLabelsToLabelable") == 1
    same = b.put("k", title="T", body="B", labels=["kind:bug"], had=had)
    assert gh.ops.count("addLabelsToLabelable") == 1 and same["labels"] == ["kind:bug"]
    more = b.put("k", title="T", body="B", labels=["area:x"], had=same)
    assert more["labels"] == ["kind:bug", "area:x"] and "removeLabelsFromLabelable" not in gh.ops


def test_attach_puts_an_issue_under_a_parent():
    gh = FakeGitHub(project=True)
    b = board_with_fields(gh)
    b.attach("I-parent", "I-child")
    assert gh.parents["I-child"] == "I-parent"


WANTED = [{"name": "Goals", "layout": "TABLE_LAYOUT", "filter": "label:goal", "fields": ["State", "Topic"]},
          {"name": "All", "layout": "BOARD_LAYOUT"}]


def view_board():
    gh = FakeGitHub(project=True)
    b = board_with_fields(gh)
    gh.views = [{"id": "V-a", "name": "View 1", "layout": "TABLE_LAYOUT", "filter": "", "fields": []},
                {"id": "V-b", "name": "Theirs", "layout": "TABLE_LAYOUT", "filter": "", "fields": []}]
    return gh, b


def test_views_come_in_order_with_their_filter_and_fields_and_only_named_ones_go():
    gh, b = view_board()
    did = b.ensure_views(WANTED, remove=("View 1",))
    assert [v["name"] for v in gh.views] == ["Theirs", "Goals", "All"]
    goals = gh.views[1]
    assert goals["filter"] == "label:goal" and goals["fields"] == ["State", "Topic"] and goals["layout"] == "TABLE_LAYOUT"
    assert did == ["made Goals", "made All", "removed View 1"]


def test_views_become_first_when_nothing_of_a_person_is_in_the_way():
    gh, b = view_board()
    gh.views = gh.views[:1]
    b.ensure_views(WANTED, remove=("View 1",))
    assert [v["name"] for v in gh.views] == ["Goals", "All"], "a view is made before any is removed"


def test_views_that_already_match_make_no_write():
    gh, b = view_board()
    gh.views = gh.views[:1]
    b.ensure_views(WANTED, remove=("View 1",))
    gh.views[0]["fields"].reverse()     # GitHub answers in the project's own field order
    writes = b.writes
    assert b.ensure_views(WANTED, remove=("View 1",)) == [] and b.writes == writes


def test_a_view_that_drifted_is_changed_in_place():
    gh, b = view_board()
    gh.views = gh.views[:1]
    b.ensure_views(WANTED, remove=("View 1",))
    gh.views[0]["filter"] = "someone changed it"
    assert b.ensure_views(WANTED) == ["changed Goals"] and gh.views[0]["filter"] == "label:goal"


def test_a_view_naming_a_field_the_project_lacks_is_refused():
    gh, b = view_board()
    with pytest.raises(BoardError, match="no field"):
        b.ensure_views([{"name": "X", "layout": "TABLE_LAYOUT", "fields": ["Nope"]}])

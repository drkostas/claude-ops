"""project_board.py — keep a GitHub Project in step with records kept somewhere else.

A program that already keeps its own list of work (tasks, requests, tickets in a database) can show
that list on a GitHub Project, where it has a board view, sub-issue trees and a phone app, without
anyone typing a task twice. This writes one issue per record into a repository, adds each issue to
one project, sets the project's fields from the record, nests issues under a parent issue, and
closes them when the record is closed. The records stay the source of truth: an edit made on
GitHub is overwritten the next time the record is written.

It writes only what changed. `put` takes the state it returned last time for that record (`had`)
and compares, so a pass over a thousand unchanged records makes no write at all. The caller keeps
that state (a row, a JSON file) beside its record.

    board = Board("owner/tracker", "Work", selects={"State": ["Open", "Done"]}, texts=("Topic",))
    board.ensure()                       # the project and its fields, made when missing
    had = board.put("task-17", title="Rotate the backup key", body="...", closed=False,
                    fields={"State": "Open", "Topic": "backups"}, had=None)
    # keep `had`; pass it next time
    board.prune(days=14)                 # closed items leave the board (the issues stay)

GitHub limits writes (about 80 a minute and 500 an hour for one account), so each Board counts its
writes and stops at `budget`, raising OutOfBudget before a record it cannot finish. A caller runs
again later and carries on where it stopped. A failure part way through a record raises BoardError
with `had` set to what was written, so the issue it made is not made twice.

Everything goes through the gh CLI (`gh api graphql`), so it uses whatever account gh is signed in
to, and needs the `project` scope for the project and `repo` for the issues. Notifications are
never sent to the person whose account writes, and `ignore_notifications()` also stops the
repository's notifications reaching that account from anyone else.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from datetime import datetime, timedelta, timezone

#: the first line of every issue body this writes, so an issue can be found by its record's key
MARKER = "<!-- board-key: {key} -->"
#: the most writes one `put` can make (issue, title or body, state, parent, project item, fields)
PUT_WRITES = 6
#: the colour a new single-select option gets
COLOR = "GRAY"


class BoardError(RuntimeError):
    """A write or read GitHub refused. `had` is the record's state as far as it was written."""

    def __init__(self, message: str, had: dict | None = None):
        super().__init__(message)
        self.had = had


class OutOfBudget(BoardError):
    """The Board has made `budget` writes; run again later."""


class RateLimited(BoardError):
    """GitHub refused a write for its rate limit; wait before writing again."""


def _gh(args: list[str], stdin: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True, env=env)


def body_with_marker(key: str, body: str) -> str:
    """Pure. The issue body as written: the marker line, then the caller's text."""
    return MARKER.format(key=key) + "\n" + body


def digest(text: str) -> str:
    """Pure. A short fingerprint of a body, kept in `had` instead of the body itself."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class Board:
    """One repository's issues shown on one project of the repository's owner.

    `selects` maps each single-select field to the options it starts with (a value not among them
    is added the first time a record uses it). `texts` names the text fields. A field that already
    exists on the project is used as it is.
    """

    def __init__(self, repo: str, title: str, *, selects: dict[str, list[str]] | None = None,
                 texts: tuple[str, ...] = (), budget: int = 60, pause: float = 1.0,
                 gh=None, env: dict | None = None, sleep=time.sleep):
        self.owner, self.name = repo.split("/", 1)
        self.repo, self.title = repo, title
        self.selects, self.texts = dict(selects or {}), tuple(texts)
        self.budget, self.pause, self.sleep = budget, pause, sleep
        self.writes = 0
        self._gh = gh or (lambda args, stdin=None: _gh(args, stdin, env))
        self.project: dict | None = None
        self.repository_id: str | None = None
        self.fields: dict[str, dict] = {}

    # -- talking to GitHub ---------------------------------------------------------------------

    def graphql(self, query: str, variables: dict | None = None, *, write: bool = False) -> dict:
        """Run one GraphQL query or mutation and return its `data`. A write counts against the
        budget and waits `pause` seconds first, so a long pass stays under GitHub's write rate."""
        if write:
            if self.writes >= self.budget:
                raise OutOfBudget(f"{self.writes} writes made, the budget is {self.budget}")
            if self.writes:
                self.sleep(self.pause)
            self.writes += 1
        out = self._gh(["api", "graphql", "--input", "-"],
                       json.dumps({"query": query, "variables": variables or {}}))
        try:
            reply = json.loads(out.stdout or "{}")
        except ValueError:
            reply = {}
        errors = reply.get("errors") or []
        if out.returncode != 0 or errors:
            words = "; ".join(e.get("message", "") for e in errors) or (out.stderr or "").strip()
            if "rate limit" in words.lower():
                raise RateLimited(words or "rate limited")
            raise BoardError(words or f"gh exited {out.returncode}")
        return reply.get("data") or {}

    def ignore_notifications(self) -> None:
        """Stop the repository's notifications reaching the account gh is signed in to."""
        out = self._gh(["api", "-X", "PUT", f"repos/{self.repo}/subscription", "-F", "ignored=true"])
        if out.returncode != 0:
            raise BoardError((out.stderr or "").strip() or "could not change the subscription")

    # -- the project and its fields ------------------------------------------------------------

    def ensure(self) -> dict:
        """Find the project by its title among the owner's projects, or make it, linked to the
        repository; then make each field that is missing. Returns the project (id, number, url)."""
        data = self.graphql(
            """query($owner: String!, $name: String!, $title: String!) {
                 repository(owner: $owner, name: $name) {
                   id
                   owner { id ... on ProjectV2Owner {
                     projectsV2(first: 20, query: $title) { nodes { id number title url } } } } } }""",
            {"owner": self.owner, "name": self.name, "title": self.title})
        repo = data.get("repository") or {}
        if not repo:
            raise BoardError(f"no repository {self.repo}")
        self.repository_id = repo["id"]
        nodes = ((repo.get("owner") or {}).get("projectsV2") or {}).get("nodes") or []
        found = [p for p in nodes if p.get("title") == self.title]
        if found:
            self.project = found[0]
        else:
            made = self.graphql(
                """mutation($owner: ID!, $title: String!, $repo: ID!) {
                     createProjectV2(input: {ownerId: $owner, title: $title, repositoryId: $repo}) {
                       projectV2 { id number title url } } }""",
                {"owner": repo["owner"]["id"], "title": self.title, "repo": self.repository_id},
                write=True)
            self.project = made["createProjectV2"]["projectV2"]
        self._read_fields()
        for name, options in self.selects.items():
            if name not in self.fields:
                self._create_field(name, "SINGLE_SELECT", options or [name])
        for name in self.texts:
            if name not in self.fields:
                self._create_field(name, "TEXT")
        return self.project

    def _read_fields(self) -> None:
        data = self.graphql(
            """query($id: ID!) { node(id: $id) { ... on ProjectV2 { fields(first: 50) { nodes {
                 ... on ProjectV2FieldCommon { id name dataType }
                 ... on ProjectV2SingleSelectField { options { id name } } } } } } }""",
            {"id": self.project["id"]})
        self.fields = {}
        for f in ((data.get("node") or {}).get("fields") or {}).get("nodes") or []:
            if f.get("name"):
                self.fields[f["name"]] = {"id": f["id"], "type": f.get("dataType"),
                                          "options": {o["name"]: o["id"] for o in f.get("options") or []}}

    def _create_field(self, name: str, kind: str, options: list[str] | None = None) -> None:
        variables = {"project": self.project["id"], "name": name, "kind": kind}
        extra = ""
        if kind == "SINGLE_SELECT":
            variables["options"] = [{"name": o, "color": COLOR, "description": ""} for o in options]
            extra = ", singleSelectOptions: $options"
        self.graphql(
            f"""mutation($project: ID!, $name: String!, $kind: ProjectV2CustomFieldType!
                         {", $options: [ProjectV2SingleSelectFieldOptionInput!]" if extra else ""}) {{
                  createProjectV2Field(input: {{projectId: $project, dataType: $kind, name: $name{extra}}}) {{
                    projectV2Field {{ ... on ProjectV2FieldCommon {{ id }} }} }} }}""",
            variables, write=True)
        self._read_fields()

    def option_id(self, field: str, value: str) -> str:
        """The id of a single-select option, adding the option when the field lacks it. GitHub
        replaces a field's whole option list, so the existing options are sent with their ids and
        keep the values already set on items."""
        f = self.fields[field]
        if value in f["options"]:
            return f["options"][value]
        options = [{"id": i, "name": n, "color": COLOR, "description": ""} for n, i in f["options"].items()]
        options.append({"name": value, "color": COLOR, "description": ""})
        self.graphql(
            """mutation($field: ID!, $options: [ProjectV2SingleSelectFieldOptionInput!]) {
                 updateProjectV2Field(input: {fieldId: $field, singleSelectOptions: $options}) {
                   projectV2Field { ... on ProjectV2SingleSelectField { id } } } }""",
            {"field": f["id"], "options": options}, write=True)
        self._read_fields()
        if value not in self.fields[field]["options"]:
            raise BoardError(f"GitHub did not add the option {value!r} to {field}")
        return self.fields[field]["options"][value]

    # -- one record ----------------------------------------------------------------------------

    def put(self, key: str, *, title: str, body: str, closed: bool = False, parent: str | None = None,
            fields: dict[str, str] | None = None, had: dict | None = None) -> dict:
        """Make the record's issue say what the record says, writing only what changed since `had`.

        `parent` is the node id of the issue this one sits under (the `node` of its `had`), or None.
        A field set to "" is cleared. Returns the new `had`, which the caller keeps.
        """
        if self.project is None:
            raise BoardError("call ensure() first")
        if self.writes + PUT_WRITES > self.budget:
            raise OutOfBudget(f"{self.writes} writes made, the budget is {self.budget}")
        new = dict(had or {})
        new.setdefault("fields", {})
        new["fields"] = dict(new["fields"])
        full = body_with_marker(key, body)
        try:
            if not new.get("node"):
                made = self.graphql(
                    """mutation($repo: ID!, $title: String!, $body: String!) {
                         createIssue(input: {repositoryId: $repo, title: $title, body: $body}) {
                           issue { id number url } } }""",
                    {"repo": self.repository_id, "title": title, "body": full}, write=True)["createIssue"]["issue"]
                new.update(node=made["id"], number=made["number"], url=made["url"],
                           title=title, body=digest(full), closed=False)
            elif new.get("title") != title or new.get("body") != digest(full):
                self.graphql(
                    """mutation($id: ID!, $title: String!, $body: String!) {
                         updateIssue(input: {id: $id, title: $title, body: $body}) { issue { id } } }""",
                    {"id": new["node"], "title": title, "body": full}, write=True)
                new.update(title=title, body=digest(full))
            if bool(new.get("closed")) != bool(closed):
                if closed:
                    self.graphql(
                        """mutation($id: ID!) { closeIssue(input: {issueId: $id, stateReason: COMPLETED}) {
                             issue { id } } }""", {"id": new["node"]}, write=True)
                else:
                    self.graphql(
                        """mutation($id: ID!) { reopenIssue(input: {issueId: $id}) { issue { id } } }""",
                        {"id": new["node"]}, write=True)
                new["closed"] = bool(closed)
            if new.get("parent") != parent:
                if parent:
                    self.graphql(
                        """mutation($parent: ID!, $child: ID!) {
                             addSubIssue(input: {issueId: $parent, subIssueId: $child, replaceParent: true}) {
                               issue { id } } }""", {"parent": parent, "child": new["node"]}, write=True)
                else:
                    self.graphql(
                        """mutation($parent: ID!, $child: ID!) {
                             removeSubIssue(input: {issueId: $parent, subIssueId: $child}) { issue { id } } }""",
                        {"parent": new["parent"], "child": new["node"]}, write=True)
                new["parent"] = parent
            if not new.get("item"):
                new["item"] = self.add_item(new["node"])
            for name, value in (fields or {}).items():
                if new["fields"].get(name) != value:
                    self._set_field(new["item"], name, value)
                    new["fields"][name] = value
        except BoardError as e:
            e.had = new
            raise
        return new

    def add_item(self, content_id: str) -> str:
        """Put an issue or pull request (by node id) on the project. Returns the item's id.

        A project's built-in workflows can add an issue first (a new project adds every sub-issue
        of an issue it holds), and GitHub then refuses the add with "Content already exists in this
        project", so the existing item is looked up instead."""
        try:
            data = self.graphql(
                """mutation($project: ID!, $content: ID!) {
                     addProjectV2ItemById(input: {projectId: $project, contentId: $content}) { item { id } } }""",
                {"project": self.project["id"], "content": content_id}, write=True)
            return data["addProjectV2ItemById"]["item"]["id"]
        except BoardError as e:
            if "already exists" not in str(e).lower():
                raise
        data = self.graphql(
            """query($id: ID!) { node(id: $id) {
                 ... on Issue { projectItems(first: 50) { nodes { id project { id } } } }
                 ... on PullRequest { projectItems(first: 50) { nodes { id project { id } } } } } }""",
            {"id": content_id})
        for it in (((data.get("node") or {}).get("projectItems") or {}).get("nodes")) or []:
            if (it.get("project") or {}).get("id") == self.project["id"]:
                return it["id"]
        raise BoardError(f"GitHub says {content_id} is on the project, but it has no item there")

    def add_pull_request(self, url: str) -> str | None:
        """Put a pull request on the project by its URL. Returns the item id, or None when the URL
        is not a pull request GitHub knows."""
        data = self.graphql("""query($url: URI!) { resource(url: $url) { ... on PullRequest { id } } }""",
                            {"url": url})
        node = (data.get("resource") or {}).get("id")
        return self.add_item(node) if node else None

    def _set_field(self, item: str, name: str, value: str) -> None:
        f = self.fields.get(name)
        if f is None:
            raise BoardError(f"the project has no field {name!r}")
        if value == "":
            self.graphql(
                """mutation($project: ID!, $item: ID!, $field: ID!) {
                     clearProjectV2ItemFieldValue(input: {projectId: $project, itemId: $item, fieldId: $field}) {
                       projectV2Item { id } } }""",
                {"project": self.project["id"], "item": item, "field": f["id"]}, write=True)
            return
        if f["type"] == "SINGLE_SELECT":
            given = {"singleSelectOptionId": self.option_id(name, value)}
        else:
            given = {"text": value}
        self.graphql(
            """mutation($project: ID!, $item: ID!, $field: ID!, $value: ProjectV2FieldValue!) {
                 updateProjectV2ItemFieldValue(input: {projectId: $project, itemId: $item, fieldId: $field,
                                                       value: $value}) { projectV2Item { id } } }""",
            {"project": self.project["id"], "item": item, "field": f["id"], "value": given}, write=True)

    # -- keeping the board small ---------------------------------------------------------------

    def prune(self, days: float, now: datetime | None = None) -> int:
        """Take off the project every issue and pull request closed more than `days` ago. The
        issues and pull requests themselves stay. Returns how many items it took off; stops
        quietly when the budget is spent, and the next run takes off the rest."""
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
        removed, after = 0, None
        while True:
            data = self.graphql(
                """query($id: ID!, $after: String) { node(id: $id) { ... on ProjectV2 {
                     items(first: 100, after: $after) {
                       pageInfo { hasNextPage endCursor }
                       nodes { id content { ... on Issue { closed closedAt }
                                            ... on PullRequest { closed closedAt } } } } } } }""",
                {"id": self.project["id"], "after": after})
            items = ((data.get("node") or {}).get("items")) or {}
            for it in items.get("nodes") or []:
                c = it.get("content") or {}
                at = c.get("closedAt")
                if c.get("closed") and at and datetime.fromisoformat(at.replace("Z", "+00:00")) < cutoff:
                    try:
                        self.graphql(
                            """mutation($project: ID!, $item: ID!) {
                                 deleteProjectV2Item(input: {projectId: $project, itemId: $item}) { deletedItemId } }""",
                            {"project": self.project["id"], "item": it["id"]}, write=True)
                    except OutOfBudget:
                        return removed
                    removed += 1
            page = items.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                return removed
            after = page.get("endCursor")

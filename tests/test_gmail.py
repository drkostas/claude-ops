import io
import json
import urllib.error
import urllib.parse

import pytest

from claude_ops.gmail import GmailError, Mailbox


class Answer:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


class Google:
    """A stand-in for urlopen: answers the token exchange and each Gmail call from `pages`, in order."""

    def __init__(self, pages=(), token="access-1"):
        self.pages, self.token, self.requests = list(pages), token, []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if req.full_url.startswith("https://oauth2.googleapis.com/token"):
            return Answer({"access_token": self.token, "expires_in": 3599})
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return Answer(page)


def files(tmp_path, token=None, client=None):
    t = tmp_path / "token.json"
    t.write_text(json.dumps(token if token is not None else {"refresh_token": "refresh-1"}))
    c = tmp_path / "credentials.json"
    c.write_text(json.dumps(client if client is not None else
                            {"installed": {"client_id": "id-1", "client_secret": "secret-1",
                                           "token_uri": "https://oauth2.googleapis.com/token"}}))
    return t, c


def query_of(req):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)


def test_the_token_exchange_sends_the_refresh_token_and_the_client_files_client(tmp_path):
    t, c = files(tmp_path)
    g = Google([{"messages": []}])
    Mailbox(t, c, fetch=g).search("x")
    sent = urllib.parse.parse_qs(g.requests[0].data.decode())
    assert sent == {"client_id": ["id-1"], "client_secret": ["secret-1"],
                    "refresh_token": ["refresh-1"], "grant_type": ["refresh_token"]}
    assert g.requests[1].get_header("Authorization") == "Bearer access-1"


def test_a_token_file_that_names_its_client_needs_no_client_file(tmp_path):
    t, _ = files(tmp_path, token={"refresh_token": "r", "client_id": "own-id", "client_secret": "own-secret"})
    g = Google([{"messages": []}])
    Mailbox(t, fetch=g).search("x")
    assert urllib.parse.parse_qs(g.requests[0].data.decode())["client_id"] == ["own-id"]


def test_a_web_client_file_is_read_too(tmp_path):
    t, c = files(tmp_path, client={"web": {"client_id": "web-id", "client_secret": "s"}})
    g = Google([{"messages": []}])
    Mailbox(t, c, fetch=g).search("x")
    assert urllib.parse.parse_qs(g.requests[0].data.decode())["client_id"] == ["web-id"]


def test_missing_refresh_token_or_client_is_refused_before_any_request(tmp_path):
    t, c = files(tmp_path, token={"access_token": "only"})
    with pytest.raises(ValueError, match="refresh_token"):
        Mailbox(t, c, fetch=Google())
    t, _ = files(tmp_path)
    with pytest.raises(ValueError, match="client file"):
        Mailbox(t, fetch=Google())


def test_search_passes_the_query_and_follows_pages_up_to_the_limit(tmp_path):
    t, c = files(tmp_path)
    g = Google([{"messages": [{"id": "a", "threadId": "ta"}, {"id": "b", "threadId": "tb"}], "nextPageToken": "p2"},
                {"messages": [{"id": "c", "threadId": "tc"}], "nextPageToken": "p3"}])
    box = Mailbox(t, c, fetch=g)
    assert [m["id"] for m in box.search('subject:"[example.org]"', max_results=3)] == ["a", "b", "c"]
    first, second = query_of(g.requests[1]), query_of(g.requests[2])
    assert first["q"] == ['subject:"[example.org]"'] and first["maxResults"] == ["3"]
    assert second["pageToken"] == ["p2"] and second["maxResults"] == ["1"]
    assert len(g.requests) == 3  # the limit was reached, so page 3 is never asked for


def test_one_access_token_per_mailbox(tmp_path):
    t, c = files(tmp_path)
    g = Google([{"messages": []}, {"messages": []}])
    box = Mailbox(t, c, fetch=g)
    box.search("a")
    box.search("b")
    assert sum(r.full_url.startswith("https://oauth2") for r in g.requests) == 1


def test_message_gives_the_named_headers_by_name(tmp_path):
    t, c = files(tmp_path)
    g = Google([{"id": "a", "threadId": "ta", "labelIds": ["INBOX"], "internalDate": "1730000000000",
                 "snippet": "Hello",
                 "payload": {"headers": [{"name": "Subject", "value": "Hi"}, {"name": "Reply-To", "value": "x@example.org"}]}}])
    m = Mailbox(t, c, fetch=g).message("a", headers=("Subject", "Reply-To"))
    assert m == {"id": "a", "threadId": "ta", "labels": ["INBOX"], "internalDate": "1730000000000",
                 "snippet": "Hello", "headers": {"Subject": "Hi", "Reply-To": "x@example.org"}}
    q = query_of(g.requests[1])
    assert q["format"] == ["metadata"] and q["metadataHeaders"] == ["Subject", "Reply-To"]


def test_message_internaldate_is_none_when_absent(tmp_path):
    t, c = files(tmp_path)
    g = Google([{"id": "a", "payload": {"headers": []}}])
    assert Mailbox(t, c, fetch=g).message("a")["internalDate"] is None


def test_a_refusal_raises_with_its_status(tmp_path):
    t, c = files(tmp_path)
    refused = urllib.error.HTTPError("https://gmail.googleapis.com/", 401, "Unauthorized", {},
                                     io.BytesIO(b'{"error": "invalid_grant"}'))
    with pytest.raises(GmailError) as e:
        Mailbox(t, c, fetch=Google([refused])).search("x")
    assert e.value.status == 401 and "invalid_grant" in str(e.value)


def test_no_answer_raises_with_status_0(tmp_path):
    t, c = files(tmp_path)
    with pytest.raises(GmailError) as e:
        Mailbox(t, c, fetch=Google([urllib.error.URLError("timed out")])).search("x")
    assert e.value.status == 0


def _b64(text):
    """As Gmail returns body data: base64url, and without padding."""
    import base64
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def test_body_walks_nested_multipart_for_the_first_text_plain_verbatim(tmp_path):
    t, c = files(tmp_path)
    payload = {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64("line one\nline two by someone\n")}},
            {"mimeType": "text/html", "body": {"data": _b64("<p>line one</p>")}},
        ]},
        {"mimeType": "application/pdf", "body": {"attachmentId": "a1"}},
    ]}
    g = Google([{"payload": payload}])
    assert Mailbox(t, c, fetch=g).body("m1") == "line one\nline two by someone\n"
    assert query_of(g.requests[1])["format"] == ["full"]  # the full message, not metadata


def test_body_falls_back_to_html_with_tags_stripped(tmp_path):
    t, c = files(tmp_path)
    html = ("<html><head><style>.a{color:red}</style></head><body>"
            "<p>Hi&nbsp;there</p><script>x=1</script><p>bye</p></body></html>")
    g = Google([{"payload": {"mimeType": "text/html", "body": {"data": _b64(html)}}}])
    assert Mailbox(t, c, fetch=g).body("m2") == "Hi there bye"


def test_body_is_empty_when_there_is_no_text_part(tmp_path):
    t, c = files(tmp_path)
    payload = {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "application/pdf", "body": {"attachmentId": "a1"}},
    ]}
    g = Google([{"payload": payload}])
    assert Mailbox(t, c, fetch=g).body("m3") == ""

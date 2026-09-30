"""
End-to-end wiring: MCP tools running over OAuth against a mocked Zendesk.

Covers all four ways this server talks to Zendesk, since each authenticates
differently and could regress independently:

* zenpy reads     — GET through the injected session
* zenpy writes    — PUT/POST through the injected session
* direct session  — attachment download, ticket search
* direct urllib   — get_tickets pagination
"""
import asyncio
import json
import urllib.request
from urllib.parse import parse_qs, urlparse
from datetime import datetime, timedelta, timezone

import pytest
import responses

from zendesk_mcp_server.tokens import TokenSet, TokenStore

SUBDOMAIN = "example"
CLIENT_ID = "zendesk-mcp-client"
API = f"https://{SUBDOMAIN}.zendesk.com/api/v2"
ACCESS_TOKEN = "oauth-access-token"
BEARER = f"Bearer {ACCESS_TOKEN}"

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
ATTACHMENT_URL = f"https://{SUBDOMAIN}.zendesk.com/attachments/token/abc/?name=x.png"

TICKET_JSON = {
    "id": 42,
    "subject": "Printer on fire",
    "description": "It is smoking",
    "status": "open",
    "priority": "urgent",
    "created_at": "2026-08-01T10:00:00Z",
    "updated_at": "2026-08-02T11:00:00Z",
    "requester_id": 7,
    "assignee_id": 9,
    "organization_id": 3,
    "tags": ["hardware"],
    "type": "incident",
}


@pytest.fixture
def oauth_server(monkeypatch, tmp_path):
    """A freshly imported server module configured for OAuth."""
    token_file = tmp_path / "tokens.json"
    TokenStore(token_file).save(
        TokenSet(
            access_token=ACCESS_TOKEN,
            subdomain=SUBDOMAIN,
            client_id=CLIENT_ID,
            refresh_token="oauth-refresh-token",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
            refresh_token_expires_at=datetime.now(timezone.utc) + timedelta(days=90),
            scope="tickets:read tickets:write ticket_attachments:read users:read hc:read",
        )
    )
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", SUBDOMAIN)
    monkeypatch.setenv("ZENDESK_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("ZENDESK_TOKEN_FILE", str(token_file))

    from zendesk_mcp_server import server as server_module

    # Reset the cached client so each test builds one from this environment.
    monkeypatch.setattr(server_module, "_zendesk_client", None)
    server_module.get_cached_kb.cache_clear()
    return server_module


def call_tool(server_module, name, arguments):
    return asyncio.run(server_module.handle_call_tool(name, arguments))


def payload_of(result):
    assert result, "tool returned no content"
    text = result[0].text
    assert not text.startswith("Error:"), text
    return json.loads(text)


def authorization_headers():
    return [
        call.request.headers.get("Authorization")
        for call in responses.calls
        if call.request.url.startswith(f"https://{SUBDOMAIN}.zendesk.com/api")
    ]


@responses.activate
def test_get_ticket_over_oauth(oauth_server):
    responses.add(responses.GET, f"{API}/tickets/42.json", json={"ticket": TICKET_JSON})

    ticket = payload_of(call_tool(oauth_server, "get_ticket", {"ticket_id": 42}))

    assert ticket["id"] == 42
    assert ticket["subject"] == "Printer on fire"
    assert ticket["tags"] == ["hardware"]
    assert authorization_headers() == [BEARER]


@responses.activate
def test_get_ticket_without_tags_returns_empty_list(oauth_server):
    untagged = {**TICKET_JSON, "tags": []}
    responses.add(responses.GET, f"{API}/tickets/42.json", json={"ticket": untagged})

    ticket = payload_of(call_tool(oauth_server, "get_ticket", {"ticket_id": 42}))

    assert ticket["tags"] == []


@responses.activate
def test_get_ticket_comments_over_oauth(oauth_server):
    responses.add(
        responses.GET,
        f"{API}/tickets/42/comments.json",
        json={
            "comments": [
                {
                    "id": 1,
                    "author_id": 7,
                    "body": "hello",
                    "html_body": "<p>hello</p>",
                    "public": True,
                    "created_at": "2026-08-01T10:05:00Z",
                    "attachments": [
                        {
                            "id": 5,
                            "file_name": "x.png",
                            "content_url": ATTACHMENT_URL,
                            "content_type": "image/png",
                            "size": 8,
                        }
                    ],
                }
            ]
        },
    )

    comments = payload_of(call_tool(oauth_server, "get_ticket_comments", {"ticket_id": 42}))

    assert comments[0]["body"] == "hello"
    assert comments[0]["attachments"][0]["content_url"] == ATTACHMENT_URL
    assert authorization_headers() == [BEARER]


@responses.activate
def test_create_ticket_comment_over_oauth(oauth_server):
    """A write path: zenpy reads the ticket, then PUTs the new comment."""
    responses.add(responses.GET, f"{API}/tickets/42.json", json={"ticket": TICKET_JSON})
    responses.add(
        responses.PUT,
        f"{API}/tickets/42.json",
        json={"ticket": TICKET_JSON, "audit": {"id": 1, "ticket_id": 42, "events": []}},
    )

    result = call_tool(
        oauth_server,
        "create_ticket_comment",
        {"ticket_id": 42, "comment": "**fixed**", "public": False},
    )

    assert "Comment created successfully" in result[0].text
    put = [c.request for c in responses.calls if c.request.method == "PUT"][0]
    assert put.headers["Authorization"] == BEARER
    # Markdown is rendered to html_body before being sent.
    assert "<strong>fixed</strong>" in json.loads(put.body)["ticket"]["comment"]["html_body"]
    assert set(authorization_headers()) == {BEARER}


@responses.activate
def test_update_ticket_over_oauth(oauth_server):
    responses.add(responses.GET, f"{API}/tickets/42.json", json={"ticket": TICKET_JSON})
    responses.add(
        responses.PUT,
        f"{API}/tickets/42.json",
        json={"ticket": TICKET_JSON, "audit": {"id": 1, "ticket_id": 42, "events": []}},
    )

    body = payload_of(
        call_tool(oauth_server, "update_ticket", {"ticket_id": 42, "status": "solved"})
    )

    assert body["ticket"]["id"] == 42
    assert set(authorization_headers()) == {BEARER}


@responses.activate
def test_update_ticket_add_tags_keeps_existing_tags(oauth_server):
    """add_tags uses the tag endpoint rather than overwriting the ticket's tag list."""
    tagged = {**TICKET_JSON, "tags": ["hardware", "claude-draft-review"]}
    responses.add(responses.PUT, f"{API}/tickets/42/tags.json", json={"tags": tagged["tags"]})
    responses.add(responses.GET, f"{API}/tickets/42.json", json={"ticket": tagged})

    body = payload_of(
        call_tool(oauth_server, "update_ticket", {"ticket_id": 42, "add_tags": ["claude-draft-review"]})
    )

    tag_put = [c.request for c in responses.calls if c.request.url.endswith("/tickets/42/tags.json")][0]
    assert tag_put.method == "PUT"
    assert json.loads(tag_put.body) == {"tags": ["claude-draft-review"]}
    # No full-ticket PUT, so the existing tags are never overwritten.
    assert not [c for c in responses.calls if c.request.method == "PUT" and c.request.url.endswith("/tickets/42.json")]
    assert body["ticket"]["tags"] == ["hardware", "claude-draft-review"]
    assert set(authorization_headers()) == {BEARER}


@responses.activate
def test_update_ticket_remove_tags_uses_tag_endpoint(oauth_server):
    responses.add(responses.DELETE, f"{API}/tickets/42/tags.json", json={"tags": []})
    responses.add(responses.GET, f"{API}/tickets/42.json", json={"ticket": {**TICKET_JSON, "tags": []}})

    body = payload_of(
        call_tool(oauth_server, "update_ticket", {"ticket_id": 42, "remove_tags": ["hardware"]})
    )

    tag_delete = [c.request for c in responses.calls if c.request.method == "DELETE"][0]
    assert tag_delete.url == f"{API}/tickets/42/tags.json"
    assert json.loads(tag_delete.body) == {"tags": ["hardware"]}
    assert body["ticket"]["tags"] == []


@responses.activate
def test_update_ticket_fields_and_add_tags_together(oauth_server):
    """Field changes go through the ticket PUT; tag additions through the tag endpoint."""
    responses.add(responses.GET, f"{API}/tickets/42.json", json={"ticket": TICKET_JSON})
    responses.add(
        responses.PUT,
        f"{API}/tickets/42.json",
        json={"ticket": TICKET_JSON, "audit": {"id": 1, "ticket_id": 42, "events": []}},
    )
    responses.add(responses.PUT, f"{API}/tickets/42/tags.json", json={"tags": ["hardware", "vip"]})

    payload_of(
        call_tool(oauth_server, "update_ticket", {"ticket_id": 42, "status": "pending", "add_tags": ["vip"]})
    )

    ticket_put = [c.request for c in responses.calls
                  if c.request.method == "PUT" and c.request.url.endswith("/tickets/42.json")][0]
    sent = json.loads(ticket_put.body)["ticket"]
    assert sent["status"] == "pending"
    assert "tags" not in sent
    tag_put = [c.request for c in responses.calls if c.request.url.endswith("/tickets/42/tags.json")][0]
    assert json.loads(tag_put.body) == {"tags": ["vip"]}


@pytest.mark.parametrize(
    "arguments, message",
    [
        ({"tags": ["a"], "add_tags": ["b"]}, "tags cannot be combined"),
        ({"tags": ["a"], "remove_tags": ["b"]}, "tags cannot be combined"),
        ({"add_tags": ["a", "b"], "remove_tags": ["b"]}, "both added and removed: b"),
    ],
)
@responses.activate
def test_update_ticket_rejects_conflicting_tag_arguments(oauth_server, arguments, message):
    result = call_tool(oauth_server, "update_ticket", {"ticket_id": 42, **arguments})

    assert result[0].text.startswith("Error:")
    assert message in result[0].text
    # Rejected before anything is sent to Zendesk.
    assert len(responses.calls) == 0


@responses.activate
def test_create_ticket_over_oauth(oauth_server):
    responses.add(
        responses.POST,
        f"{API}/tickets.json",
        json={"ticket": TICKET_JSON, "audit": {"id": 1, "ticket_id": 42, "events": []}},
        status=201,
    )
    responses.add(responses.GET, f"{API}/tickets/42.json", json={"ticket": TICKET_JSON})

    body = payload_of(
        call_tool(
            oauth_server,
            "create_ticket",
            {"subject": "Printer on fire", "description": "It is smoking"},
        )
    )

    assert body["ticket"]["subject"] == "Printer on fire"
    post = [c.request for c in responses.calls if c.request.method == "POST"][0]
    assert post.headers["Authorization"] == BEARER


@responses.activate
def test_get_ticket_attachment_over_oauth(oauth_server):
    responses.add(
        responses.GET,
        ATTACHMENT_URL,
        body=PNG_MAGIC + b"payload",
        content_type="image/png",
    )

    result = call_tool(oauth_server, "get_ticket_attachment", {"content_url": ATTACHMENT_URL})

    assert result[0].type == "image"
    assert result[0].mimeType == "image/png"
    assert responses.calls[0].request.headers["Authorization"] == BEARER


@responses.activate
def test_knowledge_base_resource_over_oauth(oauth_server):
    responses.add(
        responses.GET,
        "https://example.zendesk.com/api/v2/help_center/sections.json",
        json={"sections": [{"id": 1, "name": "FAQ", "description": "Common questions"}]},
    )
    responses.add(
        responses.GET,
        "https://example.zendesk.com/api/v2/help_center/sections/1/articles.json",
        json={
            "articles": [
                {
                    "id": 11,
                    "title": "How to reset",
                    "body": "<p>Steps</p>",
                    "updated_at": "2026-08-01T10:00:00Z",
                    "html_url": "https://example.zendesk.com/hc/en-us/articles/11",
                }
            ]
        },
    )
    from pydantic import AnyUrl

    raw = asyncio.run(oauth_server.handle_read_resource(AnyUrl("zendesk://knowledge-base")))

    body = json.loads(raw)
    assert body["metadata"]["sections"] == 1
    assert body["knowledge_base"]["FAQ"]["articles"][0]["title"] == "How to reset"
    assert set(authorization_headers()) == {BEARER}


def test_get_tickets_over_oauth(oauth_server, monkeypatch):
    """get_tickets uses urllib directly, so it is checked separately."""
    captured = {}

    class FakeResponse:
        def read(self):
            return json.dumps({"tickets": [TICKET_JSON], "next_page": None}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, *args, **kwargs):
        captured["authorization"] = request.get_header("Authorization")
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    body = payload_of(call_tool(oauth_server, "get_tickets", {"per_page": 5}))

    assert body["count"] == 1
    assert body["tickets"][0]["id"] == 42
    assert captured["authorization"] == BEARER


@responses.activate
def test_permission_error_from_zendesk_is_surfaced_not_masked(oauth_server):
    """
    A 403 from Zendesk means the operator lacks permission. It must reach the
    caller intact, since that is the whole point of per-operator OAuth.
    """
    responses.add(
        responses.GET,
        f"{API}/tickets/42.json",
        json={"error": "Forbidden", "description": "You do not have access to this ticket"},
        status=403,
    )

    result = call_tool(oauth_server, "get_ticket", {"ticket_id": 42})

    assert result[0].text.startswith("Error:")
    assert len(responses.calls) == 1, "a permissions failure must not trigger a token refresh"


def test_missing_tokens_tell_the_operator_to_bootstrap(oauth_server, tmp_path, monkeypatch):
    monkeypatch.setenv("ZENDESK_TOKEN_FILE", str(tmp_path / "absent.json"))
    monkeypatch.setattr(oauth_server, "_zendesk_client", None)

    result = call_tool(oauth_server, "get_ticket", {"ticket_id": 42})

    assert "zendesk-auth" in result[0].text


@responses.activate
def test_create_ticket_comment_defaults_to_private(oauth_server):
    """Omitting `public` posts an internal note, not a reply to the requester."""
    responses.add(responses.GET, f"{API}/tickets/42.json", json={"ticket": TICKET_JSON})
    responses.add(
        responses.PUT,
        f"{API}/tickets/42.json",
        json={"ticket": TICKET_JSON, "audit": {"id": 1, "ticket_id": 42, "events": []}},
    )

    call_tool(oauth_server, "create_ticket_comment", {"ticket_id": 42, "comment": "note"})

    put = [c.request for c in responses.calls if c.request.method == "PUT"][0]
    assert json.loads(put.body)["ticket"]["comment"]["public"] is False


SEARCH_RESULT = {
    **TICKET_JSON,
    "result_type": "ticket",
    "custom_fields": [{"id": 360003415339, "value": "12345"}],
}


@responses.activate
def test_search_tickets_over_oauth(oauth_server):
    responses.add(
        responses.GET,
        f"{API}/search.json",
        json={"results": [SEARCH_RESULT], "count": 1, "next_page": None},
    )

    body = payload_of(call_tool(
        oauth_server,
        "search_tickets",
        {"query": "status<solved requester:jo@example.com custom_field_360003415339:12345"},
    ))

    request = responses.calls[0].request
    params = parse_qs(urlparse(request.url).query)
    assert params["query"] == [
        "type:ticket status<solved requester:jo@example.com custom_field_360003415339:12345"
    ]
    assert params["per_page"] == ["25"]
    assert "sort_by" not in params, "omitting sort_by keeps Zendesk's relevance ordering"
    assert request.headers["Authorization"] == BEARER
    assert body["total"] == 1
    assert body["has_more"] is False
    ticket = body["tickets"][0]
    assert ticket["id"] == 42
    assert ticket["tags"] == ["hardware"]
    assert ticket["custom_fields"] == [{"id": 360003415339, "value": "12345"}]


@pytest.mark.parametrize(
    "query, sent",
    [
        ("type:ticket FedEx", "type:ticket FedEx"),
        # ticket_type: is a different keyword and must not suppress type:ticket.
        ("ticket_type:incident", "type:ticket ticket_type:incident"),
    ],
)
@responses.activate
def test_search_tickets_adds_type_ticket_only_when_missing(oauth_server, query, sent):
    responses.add(responses.GET, f"{API}/search.json", json={"results": [], "count": 0})

    call_tool(oauth_server, "search_tickets", {"query": query})

    assert parse_qs(urlparse(responses.calls[0].request.url).query)["query"] == [sent]


@responses.activate
def test_search_tickets_paginates_and_caps_per_page(oauth_server):
    responses.add(
        responses.GET,
        f"{API}/search.json",
        json={"results": [SEARCH_RESULT], "count": 250, "next_page": f"{API}/search.json?page=3"},
    )

    body = payload_of(call_tool(
        oauth_server,
        "search_tickets",
        {"query": "FedEx", "page": 2, "per_page": 500, "sort_by": "updated_at", "sort_order": "asc"},
    ))

    params = parse_qs(urlparse(responses.calls[0].request.url).query)
    assert params["page"] == ["2"]
    assert params["per_page"] == ["100"]
    assert params["sort_by"] == ["updated_at"]
    assert params["sort_order"] == ["asc"]
    assert body["has_more"] is True
    assert body["next_page"] == 3
    assert body["total"] == 250


@responses.activate
def test_search_tickets_surfaces_zendesk_errors(oauth_server):
    responses.add(
        responses.GET,
        f"{API}/search.json",
        json={"error": "invalid", "description": "Invalid search: too many results"},
        status=422,
    )

    result = call_tool(oauth_server, "search_tickets", {"query": "FedEx", "page": 20, "per_page": 100})

    assert result[0].text.startswith("Error:")
    assert "422" in result[0].text
    assert "too many results" in result[0].text


def test_search_tickets_requires_a_query(oauth_server):
    result = call_tool(oauth_server, "search_tickets", {"query": "  "})

    assert result[0].text.startswith("Error:")
    assert "query is required" in result[0].text

import json

import pytest

from den_hermes.orchestrator import DenWorkflowAdapter, McpHttpTools, _packet_message_id, build_mcp_adapter


class RecordingMcpTransport:
    def __init__(self, *, responses=None):
        self.responses = responses or []
        self.posts = []
        self.session_id = "session-123"

    def post(self, url, *, headers, json, timeout):  # noqa: A002 - mirrors requests API
        self.posts.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if json["method"] == "initialize":
            return FakeResponse(200, "data: " + dumps_event({"jsonrpc": "2.0", "id": json["id"], "result": {}}), {"Mcp-Session-Id": self.session_id})
        if json["method"] == "notifications/initialized":
            return FakeResponse(202, "", {})
        payload = self.responses.pop(0)
        return FakeResponse(200, "data: " + dumps_event({"jsonrpc": "2.0", "id": json["id"], "result": payload}), {})


class FakeResponse:
    def __init__(self, status_code, text, headers):
        self.status_code = status_code
        self.text = text
        self.headers = headers

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def dumps_event(payload):
    return json.dumps(payload) + "\n\n"


def test_mcp_http_tools_initializes_session_and_calls_den_tool():
    transport = RecordingMcpTransport(
        responses=[{"content": [{"type": "text", "text": json.dumps({"task": {"status": "planned"}})}]}]
    )
    tools = McpHttpTools("http://den.example/mcp", transport=transport, timeout_seconds=7)

    response = tools.mcp_den_get_task_workflow_summary(task_id=1401)

    assert response == {"task": {"status": "planned"}}
    assert [post["json"]["method"] for post in transport.posts] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]
    tool_call = transport.posts[-1]
    assert tool_call["headers"]["Mcp-Session-Id"] == "session-123"
    assert tool_call["json"]["params"] == {
        "name": "get_task_workflow_summary",
        "arguments": {"task_id": 1401},
    }


def test_mcp_http_tools_fails_closed_when_session_id_missing():
    class MissingSessionTransport(RecordingMcpTransport):
        def post(self, url, *, headers, json, timeout):  # noqa: A002
            if json["method"] == "initialize":
                return FakeResponse(200, "data: " + dumps_event({"jsonrpc": "2.0", "id": json["id"], "result": {}}), {})
            return super().post(url, headers=headers, json=json, timeout=timeout)

    tools = McpHttpTools("http://den.example/mcp", transport=MissingSessionTransport())

    with pytest.raises(RuntimeError, match="Mcp-Session-Id"):
        tools.mcp_den_get_task_workflow_summary(task_id=1401)


def test_build_mcp_adapter_returns_live_http_adapter(monkeypatch):
    monkeypatch.setenv("DEN_HERMES_MCP_URL", "http://den.example/mcp")

    adapter = build_mcp_adapter(project_id="den-hermes-bridge", requested_by="den-hermes-runner")

    assert isinstance(adapter, DenWorkflowAdapter)
    assert isinstance(adapter.tools, McpHttpTools)
    assert adapter.tools.url == "http://den.example/mcp"
    assert adapter.project_id == "den-hermes-bridge"


def test_build_mcp_adapter_requires_explicit_mcp_url(monkeypatch):
    monkeypatch.delenv("DEN_HERMES_MCP_URL", raising=False)
    monkeypatch.delenv("DEN_MCP_URL", raising=False)

    with pytest.raises(RuntimeError, match="DEN_HERMES_MCP_URL"):
        build_mcp_adapter(project_id="den-hermes-bridge", requested_by="den-hermes-runner")


def test_packet_message_id_accepts_nested_den_mcp_packet_response():
    assert _packet_message_id({"summary": "created packet", "packet": {"message_id": 5863}}) == 5863


class PlainJsonTransport(RecordingMcpTransport):
    def post(self, url, *, headers, json, timeout):  # noqa: A002
        self.posts.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if json["method"] == "initialize":
            return FakeResponse(200, json_dumps({"jsonrpc": "2.0", "id": json["id"], "result": {}}), {"Mcp-Session-Id": self.session_id})
        if json["method"] == "notifications/initialized":
            return FakeResponse(202, "", {})
        payload = self.responses.pop(0)
        return FakeResponse(200, json_dumps({"jsonrpc": "2.0", "id": json["id"], "result": payload}), {})


def json_dumps(payload):
    return json.dumps(payload)


def test_mcp_http_tools_accepts_plain_json_mcp_response():
    transport = PlainJsonTransport(
        responses=[{"content": [{"type": "text", "text": json.dumps({"review_round_id": 77})}]}]
    )
    tools = McpHttpTools("http://den.example/mcp", transport=transport)

    response = tools.mcp_den_request_review(task_id=1401)

    assert response == {"review_round_id": 77}


def test_mcp_http_tools_reports_empty_tool_text_with_tool_name():
    transport = RecordingMcpTransport(responses=[{"content": [{"type": "text", "text": ""}]}])
    tools = McpHttpTools("http://den.example/mcp", transport=transport)

    with pytest.raises(RuntimeError, match="request_review.*empty text"):
        tools.mcp_den_request_review(task_id=1401)

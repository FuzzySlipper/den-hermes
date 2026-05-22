from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry

_ADAPTER_PATH = Path(__file__).resolve().parents[1] / "plugins" / "platforms" / "den_channels" / "adapter.py"
_SPEC = importlib.util.spec_from_file_location("den_channels_adapter_under_test", _ADAPTER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_adapter_module = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _adapter_module
_SPEC.loader.exec_module(_adapter_module)
DenChannelsAdapter = _adapter_module.DenChannelsAdapter
normalize_tool_activity = _adapter_module.normalize_tool_activity
_on_pre_tool_call = _adapter_module._on_pre_tool_call
_on_post_tool_call = _adapter_module._on_post_tool_call
_ACTIVITY_CONTEXT_ENV = _adapter_module._ACTIVITY_CONTEXT_ENV
_ACTIVITY_CONTEXT_VAR = _adapter_module._ACTIVITY_CONTEXT_VAR
_ACTIVITY_STATES = _adapter_module._ACTIVITY_STATES

platform_registry.register(PlatformEntry(
    name="den_channels",
    label="Den Channels",
    adapter_factory=lambda cfg: None,
    check_fn=lambda: True,
))


class FakeGatewayClient:
    def __init__(self) -> None:
        self.delivered: list[tuple[int, dict[str, Any]]] = []
        self.failed: list[tuple[int, dict[str, Any]]] = []
        self.bindings: list[dict[str, Any]] = []

    async def upsert_adapter_binding(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.bindings.append(payload)
        return {"ok": True}

    async def claim_deliveries(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        return []

    async def mark_delivered(self, delivery_request_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self.delivered.append((delivery_request_id, payload))
        return {"ok": True}

    async def mark_failed(self, delivery_request_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self.failed.append((delivery_request_id, payload))
        return {"ok": True}


class FakeChannelsClient:
    def __init__(self) -> None:
        self.messages: dict[int, dict[str, Any]] = {}
        self.posts: list[tuple[str | int, dict[str, Any]]] = []
        self.reactions: list[tuple[str | int, dict[str, Any]]] = []
        self._next_post_id = 9000

    async def get_gateway_message(self, message_id: str | int) -> dict[str, Any]:
        return dict(self.messages[int(message_id)])

    async def post_channel_message(self, channel_id: str | int, payload: dict[str, Any]) -> dict[str, Any]:
        self.posts.append((channel_id, payload))
        self._next_post_id += 1
        return {"id": self._next_post_id}

    async def add_reaction(self, message_id: str | int, payload: dict[str, Any]) -> dict[str, Any]:
        self.reactions.append((message_id, payload))
        return {
            "id": 77,
            "channelMessageId": int(message_id),
            "reactorType": payload["reactorType"],
            "reactorIdentity": payload["reactorIdentity"],
            "reactionKey": payload["reactionKey"],
        }


def _adapter(gateway: FakeGatewayClient, channels: FakeChannelsClient) -> DenChannelsAdapter:
    return DenChannelsAdapter(
        PlatformConfig(
            enabled=True,
            token="***",
            extra={
                "gateway_url": "http://192.168.1.10:18080",
                "channels_url": "http://192.168.1.10:18080",
                "project_id": "den-hermes-bridge",
                "agent_identity": "den-mcp-runner",
                "role": "runner",
                "profile": "den-mcp-runner",
                "adapter_instance_id": "test-host:den-mcp-runner:runner:gateway",
                "start_claim_loop": False,
                "token": "test-token",
            },
        ),
        gateway_client=gateway,
        channels_client=channels,
    )


def _delivery(delivery_id: int, message_id: int, *, attempt_id: int) -> dict[str, Any]:
    return {
        "delivery_request_id": delivery_id,
        "attempt_id": attempt_id,
        "project_id": "den-hermes-bridge",
        "source_kind": "channel_message",
        "source_id": str(message_id),
        "metadata_json": json.dumps({"channel_id": 42, "channel_slug": "direct-agent-messages"}),
    }


@pytest.mark.asyncio
async def test_explicit_delivery_metadata_keeps_queued_lane_contexts_distinct() -> None:
    """Queued same-lane deliveries must not overwrite the earlier delivery handle.

    GatewayRunner processes Den Channels internal deliveries FIFO, but the adapter
    still sees multiple deliveries for the same project/channel lane. A final
    send that carries explicit delivery metadata must use that immutable delivery
    id rather than whichever lane context arrived most recently.
    """
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        100: {"id": 100, "channelId": 42, "senderIdentity": "patch", "body": "first"},
        101: {"id": 101, "channelId": 42, "senderIdentity": "planner", "body": "second"},
    }
    adapter = _adapter(gateway, channels)

    first = await adapter.delivery_to_event(_delivery(501, 100, attempt_id=701))
    second = await adapter.delivery_to_event(_delivery(502, 101, attempt_id=702))

    # Both deliveries share the same Den lane/session, as they should for durable
    # conversation continuity, but each delivery id remains separately addressable.
    assert first.source.chat_id == second.source.chat_id
    assert first.source.thread_id == second.source.thread_id

    result = await adapter.send(
        first.source.chat_id,
        "first final reply",
        metadata={"delivery_request_id": 501, "notify": True},
    )

    assert result.success is True
    assert [item[0] for item in gateway.delivered] == [501]
    posted_payload = channels.posts[-1][1]
    assert posted_payload["sourceId"] == "501"
    assert posted_payload["dedupeKey"] == "gateway-delivery:501:final"

    result = await adapter.send(
        second.source.chat_id,
        "second final reply",
        metadata={"delivery_request_id": 502, "notify": True},
    )

    assert result.success is True
    assert [item[0] for item in gateway.delivered] == [501, 502]
    posted_payload = channels.posts[-1][1]
    assert posted_payload["sourceId"] == "502"
    assert posted_payload["dedupeKey"] == "gateway-delivery:502:final"


@pytest.mark.asyncio
async def test_assistant_content_before_tool_calls_is_interim_until_final_notify_send() -> None:
    """Assistant text emitted before tool calls must not consume the final delivery.

    Hermes' interim assistant callback can surface model text from an assistant
    message that also has tool_calls. That send carries the delivery id but not
    the final-response notification marker used by BasePlatformAdapter's normal
    post-agent send path. Den Channels must keep it nonterminal so the later
    completion summary can use the delivery's final dedupe key and mark the
    Gateway delivery delivered exactly once.
    """
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        104: {"id": 104, "channelId": 42, "senderIdentity": "patch", "body": "approved go ahead"},
    }
    adapter = _adapter(gateway, channels)

    event = await adapter.delivery_to_event(_delivery(604, 104, attempt_id=804))

    interim = await adapter.send(
        event.source.chat_id,
        "Cleanup plan under the approval you just gave: ...",
        metadata={"delivery_request_id": 604},
    )

    assert interim.success is True
    assert gateway.delivered == []
    assert len(channels.posts) == 1
    interim_payload = channels.posts[-1][1]
    assert interim_payload["sourceId"] == "604"
    assert interim_payload["dedupeKey"] == "gateway-delivery:604:interim:804"
    interim_metadata = json.loads(interim_payload["metadataJson"])
    assert interim_metadata["delivery_stage"] == "interim"
    assert interim_metadata["terminal_delivery"] is False

    final = await adapter.send(
        event.source.chat_id,
        "Cleanup complete. Archived stale checkouts and stored the Den cleanup doc.",
        metadata={"delivery_request_id": 604, "notify": True},
    )

    assert final.success is True
    assert [item[0] for item in gateway.delivered] == [604]
    final_payload = channels.posts[-1][1]
    assert final_payload["sourceId"] == "604"
    assert final_payload["dedupeKey"] == "gateway-delivery:604:final"
    final_metadata = json.loads(final_payload["metadataJson"])
    assert final_metadata["delivery_stage"] == "final"
    assert final_metadata["terminal_delivery"] is True


@pytest.mark.asyncio
async def test_lane_context_without_explicit_delivery_metadata_is_interim_until_notify_send() -> None:
    """Thread-only gateway metadata must not make pre-tool text terminal.

    The live Gateway stream/interim paths often call adapter.send() with only
    the lane metadata returned by _thread_metadata_for_source() rather than a
    delivery_request_id. The adapter can still resolve the active Den delivery
    from its lane context, so lack of explicit delivery metadata must not fall
    back to gateway-delivery:<id>:final unless BasePlatformAdapter marked the
    true final send with notify=True.
    """
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        105: {
            "id": 105,
            "channelId": 42,
            "senderIdentity": "patch",
            "body": "please check the task",
            "threadRootMessageId": 5005,
        },
    }
    adapter = _adapter(gateway, channels)

    event = await adapter.delivery_to_event(_delivery(605, 105, attempt_id=805))

    interim = await adapter.send(
        event.source.chat_id,
        "I’ll check Den and then report back.",
        metadata={"thread_id": event.source.thread_id},
    )

    assert interim.success is True
    assert gateway.delivered == []
    interim_payload = channels.posts[-1][1]
    assert interim_payload["dedupeKey"] == "gateway-delivery:605:interim:805"
    interim_metadata = json.loads(interim_payload["metadataJson"])
    assert interim_metadata["delivery_stage"] == "interim"
    assert interim_metadata["terminal_delivery"] is False

    final = await adapter.send(
        event.source.chat_id,
        "Done: the task is open and assigned to runner.",
        metadata={"thread_id": event.source.thread_id, "notify": True},
    )

    assert final.success is True
    assert [item[0] for item in gateway.delivered] == [605]
    final_payload = channels.posts[-1][1]
    assert final_payload["dedupeKey"] == "gateway-delivery:605:final"
    final_metadata = json.loads(final_payload["metadataJson"])
    assert final_metadata["delivery_stage"] == "final"
    assert final_metadata["terminal_delivery"] is True


def test_den_channels_disables_generic_message_edit_streaming() -> None:
    assert DenChannelsAdapter.SUPPORTS_MESSAGE_EDITING is False


@pytest.mark.asyncio
async def test_agent_can_react_without_posting_text_reply() -> None:
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    adapter = _adapter(gateway, channels)

    result = await adapter.react_to_message(1234, "✅")

    assert result["channelMessageId"] == 1234
    assert result["reactionKey"] == "✅"
    assert channels.reactions == [(
        1234,
        {
            "reactorType": "agent",
            "reactorIdentity": "den-mcp-runner",
            "reactionKey": "✅",
        },
    )]
    assert channels.posts == []
    assert gateway.delivered == []


@pytest.mark.asyncio
async def test_activity_environment_is_bound_to_delivery_context(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        110: {"id": 110, "channelId": 42, "senderIdentity": "patch", "body": "please work"},
    }
    adapter = _adapter(gateway, channels)
    event = await adapter.delivery_to_event({
        **_delivery(701, 110, attempt_id=901),
        "task_id": 1528,
        "thread_id": 6448,
    })

    assert _ACTIVITY_CONTEXT_ENV not in os.environ
    await adapter.on_processing_start(event)
    assert _ACTIVITY_CONTEXT_ENV not in os.environ
    context = _adapter_module._activity_context()
    assert context["channelId"] == 42
    assert context["deliveryRequestId"] == 701
    assert context["agentIdentity"] == "den-mcp-runner"
    assert context["taskId"] == 1528
    assert context["hermesSessionKey"] == "agent:main:den_channels:channel:project:den-hermes-bridge:channel:42"

    await adapter.on_processing_complete(event, _adapter_module.ProcessingOutcome.FAILURE)
    assert _ACTIVITY_CONTEXT_ENV not in os.environ
    assert _adapter_module._activity_context() == {}


def test_activity_emitter_preserves_hermes_session_key(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[dict[str, Any]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> FakeResponse:
            posted.append(json)
            return FakeResponse()

    class FakeHttpx:
        Client = FakeClient

    monkeypatch.setitem(sys.modules, "httpx", FakeHttpx)

    _adapter_module._emit_activity_event(
        {
            "gatewayUrl": "http://gateway.test",
            "channelId": 42,
            "projectId": "den-hermes-bridge",
            "agentIdentity": "den-mcp-runner",
            "deliveryRequestId": 701,
            "hermesSessionKey": "project:den-hermes-bridge:channel:42",
        },
        normalize_tool_activity("terminal", {"command": "date"}),
    )

    assert posted[0]["hermesSessionKey"] == "project:den-hermes-bridge:channel:42"


def test_activity_emitter_forwards_spawned_worker_context(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[dict[str, Any]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> FakeResponse:
            posted.append(json)
            assert headers["Authorization"] == "Bearer parent-token"
            return FakeResponse()

    class FakeHttpx:
        Client = FakeClient

    monkeypatch.setitem(sys.modules, "httpx", FakeHttpx)

    _adapter_module._emit_activity_event(
        {
            "gatewayUrl": "http://gateway.test",
            "channelId": 42,
            "projectId": "den-hermes-bridge",
            "taskId": 1565,
            "threadId": 9001,
            "agentIdentity": "den-coder-profile",
            "deliveryRequestId": 701,
            "displayBlockId": "block-701",
            "parentHermesSessionKey": "parent-session",
            "parentAgentIdentity": "den-mcp-runner",
            "workerRunId": "coder-run-1",
            "workerRole": "coder",
            "token": "parent-token",
        },
        normalize_tool_activity("terminal", {"command": "date"}),
    )

    event = posted[0]
    assert event["displayBlockId"] == "block-701"
    assert event["parentHermesSessionKey"] == "parent-session"
    assert event["parentAgentIdentity"] == "den-mcp-runner"
    assert event["workerRunId"] == "coder-run-1"
    assert event["workerRole"] == "coder"
    metadata = json.loads(event["metadataJson"])
    assert metadata["workerRunId"] == "coder-run-1"
    assert metadata["workerRole"] == "coder"


def test_spawned_worker_activity_streams_and_dedupe_are_worker_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    emitted: list[tuple[dict[str, Any], dict[str, Any]]] = []
    monkeypatch.setattr(
        _adapter_module,
        "_emit_activity_event",
        lambda context, payload: emitted.append((dict(context), dict(payload))),
    )
    _ACTIVITY_STATES.clear()
    _ACTIVITY_CONTEXT_VAR.set({})

    base = {
        "gatewayUrl": "http://gateway.test",
        "channelId": 42,
        "displayBlockId": "block-701",
        "workerRole": "coder",
    }
    first = {**base, "workerRunId": "worker-a"}
    second = {**base, "workerRunId": "worker-b"}

    _ACTIVITY_CONTEXT_VAR.set(first)
    _on_pre_tool_call(tool_name="terminal", args={"command": "date"}, tool_call_id="a-1")
    _ACTIVITY_CONTEXT_VAR.set(second)
    _on_pre_tool_call(tool_name="terminal", args={"command": "date"}, tool_call_id="b-1")
    _ACTIVITY_CONTEXT_VAR.set(first)
    _on_pre_tool_call(tool_name="read_file", args={"path": "README.md"}, tool_call_id="a-2")

    payloads_by_worker = {}
    for context, payload in emitted:
        payloads_by_worker.setdefault(context["workerRunId"], []).append(payload)

    assert [payload["sequence"] for payload in payloads_by_worker["worker-a"]] == [1, 2]
    assert [payload["sequence"] for payload in payloads_by_worker["worker-b"]] == [1]
    assert payloads_by_worker["worker-a"][0]["dedupeKey"] == "activity:block-701:worker-a:coder:tool:1"
    assert payloads_by_worker["worker-b"][0]["dedupeKey"] == "activity:block-701:worker-b:coder:tool:1"
    assert payloads_by_worker["worker-a"][1]["dedupeKey"] == "activity:block-701:worker-a:coder:tool:2"


@pytest.mark.asyncio
async def test_tool_activity_context_is_isolated_between_concurrent_deliveries(monkeypatch: pytest.MonkeyPatch) -> None:
    emitted: list[tuple[dict[str, Any], dict[str, Any]]] = []
    monkeypatch.setattr(
        _adapter_module,
        "_emit_activity_event",
        lambda context, payload: emitted.append((dict(context), dict(payload))),
    )
    _ACTIVITY_STATES.clear()
    _ACTIVITY_CONTEXT_VAR.set({})

    async def run_delivery(delivery_id: int, session_key: str, tool_name: str) -> None:
        _ACTIVITY_CONTEXT_VAR.set({
            "gatewayUrl": "http://gateway.test",
            "channelId": 42,
            "projectId": "den-hermes-bridge",
            "agentIdentity": "den-mcp-runner",
            "deliveryRequestId": delivery_id,
            "hermesSessionKey": session_key,
        })
        await asyncio.sleep(0)
        _on_pre_tool_call(tool_name=tool_name, args={"name": tool_name}, tool_call_id=f"call-{delivery_id}")
        await asyncio.sleep(0)
        _on_post_tool_call(tool_name=tool_name, args={"name": tool_name}, tool_call_id=f"call-{delivery_id}")

    await asyncio.gather(
        run_delivery(701, "session-a", "terminal"),
        run_delivery(702, "session-b", "skill_view"),
    )

    contexts_by_delivery = {
        context["deliveryRequestId"]: context["hermesSessionKey"]
        for context, _payload in emitted
    }
    assert contexts_by_delivery == {701: "session-a", 702: "session-b"}


def test_tool_activity_normalization_redacts_truncates_and_counts() -> None:
    activity = normalize_tool_activity(
        "terminal",
        {"command": "python - <<'PY'\n" + "x" * 3000, "api_token": "super-secret"},
        status="started",
        count=2,
    )

    assert activity["eventType"] == "tool_call_started"
    assert activity["status"] == "started"
    assert activity["title"] == "terminal"
    assert "×2" in activity["summary"]
    assert "[REDACTED]" in activity["previewJson"]
    assert "super-secret" not in activity["previewJson"]
    assert len(activity["previewJson"]) <= 1400


def test_tool_activity_hooks_coalesce_adjacent_duplicate_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    emitted: list[dict[str, Any]] = []
    monkeypatch.setenv(_ACTIVITY_CONTEXT_ENV, json.dumps({
        "gatewayUrl": "http://gateway.test",
        "channelId": 42,
        "projectId": "den-hermes-bridge",
        "agentIdentity": "den-mcp-runner",
        "deliveryRequestId": 1528,
        "sessionKey": "session-1528",
    }))
    monkeypatch.setattr(_adapter_module, "_emit_activity_event", lambda context, payload: emitted.append(payload))
    _ACTIVITY_STATES.clear()

    _on_pre_tool_call(tool_name="skill_view", args={"name": "den-mcp"}, tool_call_id="call-1")
    _on_pre_tool_call(tool_name="skill_view", args={"name": "den-mcp"}, tool_call_id="call-2")
    _on_post_tool_call(tool_name="skill_view", args={"name": "den-mcp"}, tool_call_id="call-2", duration_ms=12)

    assert len(emitted) == 3
    assert emitted[0]["sequence"] == emitted[1]["sequence"] == emitted[2]["sequence"]
    assert emitted[0]["dedupeKey"] == emitted[1]["dedupeKey"] == emitted[2]["dedupeKey"]
    assert "×2" in emitted[1]["summary"]
    assert emitted[-1]["status"] == "completed"
    assert "duration_ms" in emitted[-1]["metadataJson"]


def test_tool_activity_hook_failures_are_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ACTIVITY_CONTEXT_ENV, json.dumps({
        "gatewayUrl": "http://gateway.test",
        "channelId": 42,
        "deliveryRequestId": 1528,
        "sessionKey": "session-1528",
    }))

    def boom(context: dict[str, Any], payload: dict[str, Any]) -> None:
        raise RuntimeError("activity sink down")

    monkeypatch.setattr(_adapter_module, "_emit_activity_event", boom)
    _ACTIVITY_STATES.clear()

    # Hook errors must not bubble into Hermes tool execution.
    _on_pre_tool_call(tool_name="terminal", args={"command": "date"}, tool_call_id="call-3")


@pytest.mark.asyncio
async def test_binding_payload_advertises_internal_busy_queue_observability_policy() -> None:
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    adapter = _adapter(gateway, channels)

    assert await adapter.connect() is True

    capabilities = json.loads(gateway.bindings[-1]["capabilities_json"])
    assert capabilities["durable_sessions"] is True
    assert capabilities["busy_delivery_policy"] == "force_queue_internal_no_busy_ack"
    assert capabilities["pending_delivery_observability"] == [
        "gateway_status.active_sessions.queued_events",
        "gateway_status.queued_events",
    ]
    assert capabilities["safe_pending_notifications"] == "status_only_no_mid_generation_injection"

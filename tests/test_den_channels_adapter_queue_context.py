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
from gateway.session import SessionSource, build_session_key
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
        self.completed: list[tuple[int, dict[str, Any]]] = []
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

    async def mark_completed(self, delivery_request_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self.completed.append((delivery_request_id, payload))
        return {"ok": True}

    async def mark_failed(self, delivery_request_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self.failed.append((delivery_request_id, payload))
        return {"ok": True}


class FakeChannelsClient:
    def __init__(self) -> None:
        self.messages: dict[int, dict[str, Any]] = {}
        self.events_by_channel: dict[int, dict[str, Any]] = {}
        self.event_requests: list[tuple[int, int, int]] = []
        self.posts: list[tuple[str | int, dict[str, Any]]] = []
        self.reactions: list[tuple[str | int, dict[str, Any]]] = []
        self._next_post_id = 9000

    async def get_direct_agent_events(self, *, channel_id: int, after_id: int = 0, limit: int = 10) -> dict[str, Any]:
        self.event_requests.append((channel_id, after_id, limit))
        return dict(self.events_by_channel.get(channel_id, {"items": [], "nextAfterId": after_id}))

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


def _delivery(delivery_id: int, message_id: int, *, attempt_id: int, session_id: str = "session-42") -> dict[str, Any]:
    return {
        "delivery_request_id": delivery_id,
        "attempt_id": attempt_id,
        "session_id": session_id,
        "project_id": "den-hermes-bridge",
        "source_kind": "channel_message",
        "source_id": str(message_id),
        "metadata_json": json.dumps({"channel_id": 42, "channel_slug": "direct-agent-messages", "session_scope": "source_lane"}),
    }


@pytest.mark.asyncio
async def test_direct_agent_delivery_body_takes_precedence_over_generated_summary() -> None:
    """Direct-agent wake summaries are delivery evidence, not user message text."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    adapter = _adapter(gateway, channels)

    event = await adapter.delivery_to_event({
        "delivery_request_id": 2225,
        "attempt_id": 1,
        "session_id": "session-direct-agent",
        "project_id": "den-hermes-bridge",
        "source_kind": "wake_event",
        "source_id": "direct-agent-message:672:den-mcp-planner:0e0261578baa4fc48a7c6e919da84177",
        "body": "1939 finished, is it still coming through goofy?",
        "context_summary": "Direct agent request to den-mcp-planner: recorded, pending claim/completion",
        "metadata_json": json.dumps({
            "channel_id": 672,
            "channel_slug": "den-system",
            "sender_identity": "Patch",
        }),
    })

    assert event.text == "1939 finished, is it still coming through goofy?"
    assert "recorded, pending claim/completion" not in event.text
    assert event.raw_message["context_summary"] == "Direct agent request to den-mcp-planner: recorded, pending claim/completion"
    assert event.source.chat_id == "owner:test-host:den-mcp-runner:runner:gateway"


@pytest.mark.asyncio
async def test_direct_agent_event_to_delivery_preserves_body_and_summary_evidence() -> None:
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    adapter = _adapter(gateway, channels)

    delivery = adapter._event_to_delivery({
        "id": 2227,
        "channelId": 672,
        "sourceProjectId": "den-web",
        "sourceKind": "wake_event",
        "sourceId": "direct-agent-message:672:den-mcp-runner:abc",
        "senderIdentity": "Patch",
        "summary": "Direct agent request to den-mcp-runner: recorded, pending claim/completion",
        "body": "actual human request body",
        "metadataJson": json.dumps({"sender_identity": "Patch", "channel_slug": "den-system"}),
    })

    event = await adapter.delivery_to_event(delivery)

    assert delivery["body"] == "actual human request body"
    assert delivery["context_summary"] == "Direct agent request to den-mcp-runner: recorded, pending claim/completion"
    assert event.text == "actual human request body"
    assert event.raw_message["context_summary"] == delivery["context_summary"]


@pytest.mark.asyncio
async def test_direct_agent_event_poller_filters_target_and_advances_cursor() -> None:
    channels = FakeChannelsClient()
    channels.events_by_channel[672] = {
        "items": [
            {"id": 10, "channelId": 672, "memberIdentity": "someone-else", "body": "ignore"},
            {"id": 11, "channelId": 672, "memberIdentity": "den-mcp-runner", "body": "deliver"},
            {"id": 12, "channelId": 672, "sourceId": "direct-agent-message:672:den-mcp-runner:abc", "body": "also deliver"},
        ],
        "nextAfterId": 12,
    }
    poller = _adapter_module._DirectAgentEventPoller(channels, "den-mcp-runner")

    first = await poller.poll(672, limit=10)
    second = await poller.poll(672, limit=10)

    assert [event["id"] for event in first] == [11, 12]
    assert second == []
    assert channels.event_requests == [(672, 0, 10), (672, 12, 10)]


@pytest.mark.asyncio
async def test_configured_poll_channel_ids_are_used_for_worker_pool_control() -> None:
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    adapter = DenChannelsAdapter(
        PlatformConfig(
            enabled=True,
            token="***",
            extra={
                "channels_url": "http://192.168.1.10:18081",
                "project_id": "",
                "agent_identity": "spawned-coder",
                "role": "coder",
                "profile": "spawned-coder",
                "adapter_instance_id": "test-host:spawned-coder:coder:worker",
                "poll_channel_ids": "604, 604, 672, not-an-int, -2",
                "start_claim_loop": False,
                "start_poll_loop": False,
            },
        ),
        gateway_client=gateway,
        channels_client=channels,
    )

    first = await adapter._resolve_poll_channels()
    second = await adapter._resolve_poll_channels()

    assert first == [604, 672]
    assert second == [604, 672]


@pytest.mark.asyncio
async def test_channels_only_connect_does_not_require_gateway_binding() -> None:
    channels = FakeChannelsClient()
    adapter = DenChannelsAdapter(
        PlatformConfig(
            enabled=True,
            token="***",
            extra={
                "channels_url": "http://192.168.1.10:18081",
                "project_id": "",
                "agent_identity": "den-mcp-runner",
                "role": "runner",
                "profile": "den-mcp-runner",
                "adapter_instance_id": "test-host:den-mcp-runner:runner:gateway",
                "start_claim_loop": False,
                "start_poll_loop": False,
            },
        ),
        channels_client=channels,
    )

    assert await adapter.connect() is True
    assert adapter.gateway_client is None


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
    assert [item[0] for item in gateway.completed] == [501]
    assert gateway.delivered == []
    posted_payload = channels.posts[-1][1]
    assert posted_payload["sourceId"] == "501"
    assert posted_payload["dedupeKey"] == "gateway-delivery:501:final"
    completed_payload = gateway.completed[0][1]
    assert completed_payload.get("ack_kind") == "hermes_final_reply_posted"

    result = await adapter.send(
        second.source.chat_id,
        "second final reply",
        metadata={"delivery_request_id": 502, "notify": True},
    )

    assert result.success is True
    assert [item[0] for item in gateway.completed] == [501, 502]
    assert gateway.delivered == []
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
    assert gateway.completed == []
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
    assert [item[0] for item in gateway.completed] == [604]
    assert gateway.delivered == []
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
        "I'll check Den and then report back.",
        metadata={"thread_id": event.source.thread_id},
    )

    assert interim.success is True
    assert gateway.delivered == []
    assert gateway.completed == []
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
    assert [item[0] for item in gateway.completed] == [605]
    assert gateway.delivered == []
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

    result = await adapter.react_to_message(1234, "\u2705")

    assert result["channelMessageId"] == 1234
    assert result["reactionKey"] == "\u2705"
    assert channels.reactions == [(
        1234,
        {
            "reactorType": "agent",
            "reactorIdentity": "den-mcp-runner",
            "reactionKey": "\u2705",
        },
    )]
    assert channels.posts == []
    assert gateway.delivered == []
    assert gateway.completed == []


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


def test_activity_emitter_accepts_channels_url_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Activity emission works with only channels_url (no gatewayUrl)."""
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
            posted.append((url, json))
            return FakeResponse()

    class FakeHttpx:
        Client = FakeClient

    monkeypatch.setitem(sys.modules, "httpx", FakeHttpx)

    _adapter_module._emit_activity_event(
        {
            "channels_url": "http://channels.test",
            "channelId": 42,
            "projectId": "den-hermes-bridge",
            "agentIdentity": "den-mcp-runner",
            "deliveryRequestId": 702,
        },
        normalize_tool_activity("terminal", {"command": "date"}),
    )

    assert len(posted) == 1
    url, body = posted[0]
    assert url == "http://channels.test/api/channel-activity-events"
    assert body["channelId"] == "42"
    assert body["agentIdentity"] == "den-mcp-runner"


def test_activity_emitter_prefers_channels_over_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """When both channels_url and gatewayUrl are set, channels_url is preferred."""
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
            posted.append((url, json))
            return FakeResponse()

    class FakeHttpx:
        Client = FakeClient

    monkeypatch.setitem(sys.modules, "httpx", FakeHttpx)

    _adapter_module._emit_activity_event(
        {
            "channelsUrl": "http://channels.test",
            "gatewayUrl": "http://gateway.test",
            "channelId": 42,
            "projectId": "den-hermes-bridge",
            "agentIdentity": "den-mcp-runner",
        },
        normalize_tool_activity("terminal", {"command": "date"}),
    )

    assert len(posted) == 1
    url, _body = posted[0]
    assert url == "http://channels.test/api/channel-activity-events"
    assert "gateway.test" not in url


def test_activity_emitter_falls_back_to_gateway_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """When only gatewayUrl is set (no channels_url), gatewayUrl is used as fallback."""
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
            posted.append((url, json))
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
        },
        normalize_tool_activity("terminal", {"command": "date"}),
    )

    assert len(posted) == 1
    url, _body = posted[0]
    assert url == "http://gateway.test/api/channel-activity-events"


def test_canonical_spawned_worker_activity_payload_shape_1567(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cross-repo invariant for spawned-worker activity grouping payloads.

    Gateway/Channels tests assert the same camelCase shape: child profile
    identities emit tool activity into the parent display block, with worker
    identity repeated in metadataJson for consumers that only inspect metadata.
    """
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
            "taskId": 1567,
            "threadId": 9001,
            "agentIdentity": "den-coder-profile",
            "deliveryRequestId": 701,
            "displayBlockId": "parent-1567",
            "parentHermesSessionKey": "parent-session-1567",
            "parentAgentIdentity": "den-mcp-runner",
            "workerRunId": "coder-1567",
            "workerRole": "coder",
        },
        normalize_tool_activity("terminal", {"command": "git status --short"}),
    )

    event = posted[0]
    expected_shape = {
        "agentIdentity": "den-coder-profile",
        "deliveryRequestId": "701",
        "displayBlockId": "parent-1567",
        "parentHermesSessionKey": "parent-session-1567",
        "parentAgentIdentity": "den-mcp-runner",
        "workerRunId": "coder-1567",
        "workerRole": "coder",
    }
    for key, expected in expected_shape.items():
        assert event[key] == expected
    assert "displayDeliveryRequestId" not in event
    metadata = json.loads(event["metadataJson"])
    assert metadata["workerRunId"] == "coder-1567"
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
        "displayBlockId": "parent-1567",
        "parentHermesSessionKey": "parent-session-1567",
        "parentAgentIdentity": "den-mcp-runner",
        "agentIdentity": "den-coder-profile",
        "workerRole": "coder",
    }
    first = {**base, "workerRunId": "coder-1567"}
    second = {**base, "workerRunId": "reviewer-1567", "workerRole": "reviewer", "agentIdentity": "den-reviewer-profile"}

    _ACTIVITY_CONTEXT_VAR.set(first)
    _on_pre_tool_call(tool_name="terminal", args={"command": "date"}, tool_call_id="a-1")
    _ACTIVITY_CONTEXT_VAR.set(second)
    _on_pre_tool_call(tool_name="terminal", args={"command": "date"}, tool_call_id="b-1")
    _ACTIVITY_CONTEXT_VAR.set(first)
    _on_pre_tool_call(tool_name="read_file", args={"path": "README.md"}, tool_call_id="a-2")

    payloads_by_worker = {}
    for context, payload in emitted:
        payloads_by_worker.setdefault(context["workerRunId"], []).append(payload)

    assert [payload["sequence"] for payload in payloads_by_worker["coder-1567"]] == [1, 2]
    assert [payload["sequence"] for payload in payloads_by_worker["reviewer-1567"]] == [1]
    assert payloads_by_worker["coder-1567"][0]["dedupeKey"] == "activity:parent-1567:coder-1567:coder:tool:1"
    assert payloads_by_worker["reviewer-1567"][0]["dedupeKey"] == "activity:parent-1567:reviewer-1567:reviewer:tool:1"
    assert payloads_by_worker["coder-1567"][1]["dedupeKey"] == "activity:parent-1567:coder-1567:coder:tool:2"
    contexts_by_worker = {context["workerRunId"]: context for context, _payload in emitted}
    assert contexts_by_worker["coder-1567"]["agentIdentity"] == "den-coder-profile"
    assert contexts_by_worker["reviewer-1567"]["agentIdentity"] == "den-reviewer-profile"
    assert contexts_by_worker["coder-1567"]["displayBlockId"] == "parent-1567"
    assert contexts_by_worker["reviewer-1567"]["displayBlockId"] == "parent-1567"
    assert contexts_by_worker["coder-1567"]["parentHermesSessionKey"] == "parent-session-1567"
    assert contexts_by_worker["reviewer-1567"]["parentAgentIdentity"] == "den-mcp-runner"


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
    assert "\u00d72" in activity["summary"]
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
    assert "\u00d72" in emitted[1]["summary"]
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


@pytest.mark.asyncio
async def test_final_send_calls_complete_with_ack_kind() -> None:
    """A final visible reply must call mark_completed with ack_kind and not only mark_delivered."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        106: {"id": 106, "channelId": 42, "senderIdentity": "runner", "body": "please complete"},
    }
    adapter = _adapter(gateway, channels)
    event = await adapter.delivery_to_event(_delivery(606, 106, attempt_id=806))

    result = await adapter.send(
        event.source.chat_id,
        "Final reply payload.",
        metadata={"delivery_request_id": 606, "notify": True},
    )

    assert result.success is True
    assert len(gateway.completed) == 1
    assert gateway.delivered == []
    completed_id, completed_payload = gateway.completed[0]
    assert completed_id == 606
    assert completed_payload.get("ack_kind") == "hermes_final_reply_posted"
    assert completed_payload.get("attempt_id") == 806
    assert completed_payload.get("adapter_kind") == "hermes_profile"
    assert completed_payload.get("session_id") is not None
    assert completed_payload.get("external_message_id") is not None


@pytest.mark.asyncio
async def test_interim_send_does_not_complete() -> None:
    """Interim sends must not call mark_completed or mark_delivered."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        107: {"id": 107, "channelId": 42, "senderIdentity": "runner", "body": "interim test"},
    }
    adapter = _adapter(gateway, channels)
    event = await adapter.delivery_to_event(_delivery(607, 107, attempt_id=807))

    result = await adapter.send(
        event.source.chat_id,
        "Thinking...",
        metadata={"delivery_request_id": 607},
    )

    assert result.success is True
    assert gateway.completed == []
    assert gateway.delivered == []


@pytest.mark.asyncio
async def test_processing_no_response_calls_fail_not_complete() -> None:
    """When processing succeeds without a visible reply, the adapter must call
    mark_failed and must NOT call mark_completed."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        108: {"id": 108, "channelId": 42, "senderIdentity": "runner", "body": "silent process"},
    }
    adapter = _adapter(gateway, channels)
    event = await adapter.delivery_to_event(_delivery(608, 108, attempt_id=808))
    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, _adapter_module.ProcessingOutcome.SUCCESS)

    assert len(gateway.failed) == 1
    assert gateway.failed[0][0] == 608
    assert gateway.completed == []
    assert gateway.delivered == []


@pytest.mark.asyncio
async def test_processing_failure_calls_fail_not_complete() -> None:
    """Processing failure must call mark_failed and not call mark_completed."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        109: {"id": 109, "channelId": 42, "senderIdentity": "runner", "body": "fail me"},
    }
    adapter = _adapter(gateway, channels)
    event = await adapter.delivery_to_event(_delivery(609, 109, attempt_id=809))
    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, _adapter_module.ProcessingOutcome.FAILURE)

    assert len(gateway.failed) == 1
    assert gateway.failed[0][0] == 609
    assert gateway.completed == []
    assert gateway.delivered == []


def test_direct_agent_message_handler_checks_required_member_identity() -> None:
    """The handler must reject calls without member_identity."""
    result = asyncio.run(_adapter_module._handle_direct_agent_message(body="hello"))
    parsed = json.loads(result)
    assert parsed["status"] == "error"
    assert "member_identity" in parsed.get("error", "")


def test_direct_agent_message_handler_checks_required_body() -> None:
    """The handler must reject calls without body."""
    result = asyncio.run(_adapter_module._handle_direct_agent_message(member_identity="test-agent"))
    parsed = json.loads(result)
    assert parsed["status"] == "error"
    assert "body" in parsed.get("error", "")


def test_direct_agent_message_handler_checks_channel_or_project() -> None:
    """The handler must reject calls without channel_id or project_id."""
    result = asyncio.run(_adapter_module._handle_direct_agent_message(
        member_identity="test-agent", body="hello"
    ))
    parsed = json.loads(result)
    assert parsed["status"] == "error"
    assert "channel_id" in parsed.get("error", "") or "project_id" in parsed.get("error", "")


def test_direct_agent_message_handler_checks_config_availability(monkeypatch: pytest.MonkeyPatch) -> None:
    """The handler must report error when no env, activity, or adapter-config URL is available."""
    monkeypatch.delenv("DEN_CHANNELS_URL", raising=False)
    monkeypatch.delenv("DEN_GATEWAY_URL", raising=False)
    _adapter_module._DIRECT_AGENT_CONFIG_DEFAULTS.clear()
    _ACTIVITY_CONTEXT_VAR.set({})

    result = asyncio.run(_adapter_module._handle_direct_agent_message(
        member_identity="test-agent", body="hello", channel_id=42
    ))
    parsed = json.loads(result)
    assert parsed["status"] == "error"
    assert parsed.get("error") == "DEN_CHANNELS_URL or DEN_GATEWAY_URL is not configured"


def test_direct_agent_message_handler_available_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """check_fn must account for env and adapter-config defaults."""
    monkeypatch.delenv("DEN_CHANNELS_URL", raising=False)
    monkeypatch.delenv("DEN_GATEWAY_URL", raising=False)
    _adapter_module._DIRECT_AGENT_CONFIG_DEFAULTS.clear()
    assert _adapter_module._check_direct_agent_message_available() is False
    monkeypatch.setenv("DEN_CHANNELS_URL", "http://test:8080")
    assert _adapter_module._check_direct_agent_message_available() is True
    monkeypatch.delenv("DEN_CHANNELS_URL", raising=False)
    _adapter_module._remember_direct_agent_config(channels_url="http://profile-config.test")
    assert _adapter_module._check_direct_agent_message_available() is True




def test_direct_agent_message_handler_uses_adapter_config_and_defaults_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct-agent tool must work from platform config defaults, not prompt-level shell env."""
    monkeypatch.delenv("DEN_CHANNELS_URL", raising=False)
    monkeypatch.delenv("DEN_GATEWAY_URL", raising=False)
    monkeypatch.delenv("DEN_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("DEN_CHANNELS_TOKEN", raising=False)
    _adapter_module._DIRECT_AGENT_CONFIG_DEFAULTS.clear()
    _adapter_module._remember_direct_agent_config(
        channels_url="http://channels.test",
        token="secret-token",
        agent_identity="profile-runner",
    )

    captured: dict[str, Any] = {}

    class FakeResponse:
        content = b"{}"

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return {
                "status": "recorded",
                "messageId": 123,
                "memberIdentity": "reviewer",
            }

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            return None

        async def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> FakeResponse:
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(_adapter_module._handle_direct_agent_message(
        channel_id=569,
        member_identity="reviewer",
        body="please reply",
    ))
    parsed = json.loads(result)

    assert parsed["status"] == "ok"
    assert captured["url"] == "http://channels.test/api/gateway/direct-agent-messages"
    assert captured["json"] == {
        "channelId": 569,
        "memberIdentity": "reviewer",
        "senderIdentity": "profile-runner",
        "body": "please reply",
    }
    assert captured["headers"]["Authorization"] == "Bearer secret-token"
    assert "secret-token" not in result


def test_direct_agent_message_handler_accepts_registry_args_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermes registry dispatch passes the tool argument dict positionally."""
    monkeypatch.delenv("DEN_CHANNELS_URL", raising=False)
    monkeypatch.delenv("DEN_GATEWAY_URL", raising=False)
    monkeypatch.delenv("DEN_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("DEN_CHANNELS_TOKEN", raising=False)
    _adapter_module._DIRECT_AGENT_CONFIG_DEFAULTS.clear()
    _adapter_module._remember_direct_agent_config(
        channels_url="http://channels.test",
        agent_identity="profile-runner",
    )

    captured: dict[str, Any] = {}

    class FakeResponse:
        content = b"{}"

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return {"status": "recorded", "messageId": 456}

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
            return None

        async def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> FakeResponse:
            captured["url"] = url
            captured["json"] = json
            return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(_adapter_module._handle_direct_agent_message({
        "channel_id": 569,
        "member_identity": "reviewer",
        "body": "please reply",
    }))
    parsed = json.loads(result)

    assert parsed["status"] == "ok"
    assert captured["url"] == "http://channels.test/api/gateway/direct-agent-messages"
    assert captured["json"] == {
        "channelId": 569,
        "memberIdentity": "reviewer",
        "senderIdentity": "profile-runner",
        "body": "please reply",
    }


@pytest.mark.asyncio
async def test_same_channel_different_senders_share_session_key() -> None:
    """Different senders in the same Den Channels lane must share a session key.

    Session keys must not include a sender/user suffix so channel lanes do not
    fork by sender.  All participants in a channel see one continuous session.
    """
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        200: {"id": 200, "channelId": 42, "senderIdentity": "patch", "body": "hello from patch"},
        201: {"id": 201, "channelId": 42, "senderIdentity": "planner", "body": "hello from planner"},
    }
    adapter = _adapter(gateway, channels)

    event_from_patch = await adapter.delivery_to_event(_delivery(801, 200, attempt_id=901))
    event_from_planner = await adapter.delivery_to_event(_delivery(802, 201, attempt_id=902))

    # Same channel -> same session key regardless of sender
    assert event_from_patch.source.chat_id == event_from_planner.source.chat_id
    assert event_from_patch.source.chat_type == "channel"
    assert event_from_planner.source.chat_type == "channel"

    # Build session keys the same way the adapter does internally
    session_key_patch = build_session_key(event_from_patch.source, group_sessions_per_user=False)
    session_key_planner = build_session_key(event_from_planner.source, group_sessions_per_user=False)
    assert session_key_patch == session_key_planner, \
        f"Session keys must match for same channel: {session_key_patch!r} != {session_key_planner!r}"

    # The key should not reference either sender
    assert "patch" not in session_key_patch
    assert "planner" not in session_key_patch

    # source.user_id should be None (our scoping fix)
    assert event_from_patch.source.user_id is None
    assert event_from_planner.source.user_id is None

    # source.user_name is still set for display purposes
    assert event_from_patch.source.user_name == "patch"
    assert event_from_planner.source.user_name == "planner"


@pytest.mark.asyncio
async def test_different_channels_produce_different_session_keys() -> None:
    """Different Den Channels must produce different session keys."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        300: {"id": 300, "channelId": 42, "senderIdentity": "user", "body": "in channel 42"},
        400: {"id": 400, "channelId": 99, "senderIdentity": "user", "body": "in channel 99"},
    }
    adapter = _adapter(gateway, channels)

    # Override channel_id in delivery metadata
    event_a = await adapter.delivery_to_event({
        **_delivery(901, 300, attempt_id=1001),
        "metadata_json": json.dumps({"channel_id": 42, "channel_slug": "team-a", "session_scope": "source_lane"}),
    })
    event_b = await adapter.delivery_to_event({
        **_delivery(902, 400, attempt_id=1002),
        "metadata_json": json.dumps({"channel_id": 99, "channel_slug": "team-b", "session_scope": "source_lane"}),
    })

    assert event_a.source.chat_id != event_b.source.chat_id

    session_key_a = build_session_key(event_a.source, group_sessions_per_user=False)
    session_key_b = build_session_key(event_b.source, group_sessions_per_user=False)
    assert session_key_a != session_key_b, \
        f"Session keys must differ for different channels: {session_key_a!r} == {session_key_b!r}"


@pytest.mark.asyncio
async def test_thread_lane_produces_distinct_session_key() -> None:
    """Thread/task lanes in Den Channels must produce thread-qualified session keys
    distinct from the parent channel lane."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        500: {"id": 500, "channelId": 42, "senderIdentity": "user", "body": "root channel msg"},
        501: {
            "id": 501,
            "channelId": 42,
            "senderIdentity": "user",
            "body": "thread reply",
            "threadRootMessageId": 5005,
        },
    }
    adapter = _adapter(gateway, channels)

    root_event = await adapter.delivery_to_event(_delivery(1001, 500, attempt_id=1101))
    thread_event = await adapter.delivery_to_event({
        **_delivery(1002, 501, attempt_id=1102),
        "metadata_json": json.dumps({
            "channel_id": 42,
            "channel_slug": "team-a",
            "thread_root_message_id": 5005,
        }),
    })

    assert root_event.source.chat_type == "channel"
    assert thread_event.source.chat_type == "thread"
    assert root_event.source.thread_id is None
    assert thread_event.source.thread_id is not None

    root_key = build_session_key(root_event.source, group_sessions_per_user=False)
    thread_key = build_session_key(thread_event.source, group_sessions_per_user=False)
    assert root_key != thread_key, \
        "Thread session key must differ from channel session key"
    assert "5005" in thread_key, \
        "Thread session key should reference the thread root"

    # Channel-only key must NOT contain thread reference
    assert "5005" not in root_key


def test_den_channels_session_scoping_note_exists() -> None:
    """The developer/operator note about Den Channels session scoping should exist."""
    note_path = Path(__file__).resolve().parents[1] / "docs" / "den-channels-session-scoping-1719.md"
    assert note_path.exists(), "Session scoping doc is missing"


def test_direct_agent_message_tool_schema_has_no_sourceKind_gateway_delivery() -> None:
    """The direct-agent message schema must not reference sourceKind=gateway_delivery
    to avoid misuse as a post_message replacement."""
    schema = _adapter_module._DIRECT_AGENT_MESSAGE_SCHEMA
    schema_text = json.dumps(schema)
    assert "gateway_delivery" not in schema_text
    assert "sourceKind" not in schema_text
    assert schema["parameters"]["properties"]["member_identity"]["type"] == "string"
    assert "member_identity" in schema["parameters"].get("required", [])
    assert "parameters" in schema


# ---------------------------------------------------------------------------
# Pool-member-aware binding / claim / activity tests (#1876)
# ---------------------------------------------------------------------------

def _adapter_with_pool_member(
    gateway: FakeGatewayClient,
    channels: FakeChannelsClient,
    *,
    pool_member_id: str = "pool-coder-02",
) -> DenChannelsAdapter:
    """Build an adapter configured with a concrete pool member identity."""
    return DenChannelsAdapter(
        PlatformConfig(
            enabled=True,
            token="***",
            extra={
                "gateway_url": "http://192.168.1.10:18080",
                "channels_url": "http://192.168.1.10:18080",
                "project_id": "den-hermes-bridge",
                "agent_identity": "spawned-coder",
                "role": "coder",
                "profile": "spawned-coder",
                "adapter_instance_id": "hermes:den-k8:spawned-coder:pool-coder-02:live",
                "pool_member_id": pool_member_id,
                "start_claim_loop": False,
                "token": "test-token",
            },
        ),
        gateway_client=gateway,
        channels_client=channels,
    )


@pytest.mark.asyncio
async def test_binding_payload_includes_pool_member_id_when_configured() -> None:
    """Adapter binding must advertise pool_member_id when configured."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    adapter = _adapter_with_pool_member(gateway, channels)

    assert await adapter.connect() is True
    binding = gateway.bindings[-1]

    assert binding["pool_member_id"] == "pool-coder-02"
    assert binding["agent_instance_id"] == "hermes:den-k8:spawned-coder:pool-coder-02:live"
    assert binding["adapter_instance_id"] == "hermes:den-k8:spawned-coder:pool-coder-02:live"
    assert binding["agent_identity"] == "spawned-coder"
    assert binding["profile"] == "spawned-coder"


@pytest.mark.asyncio
async def test_binding_payload_omits_pool_member_id_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Generic spawned-coder binding must not include pool_member_id."""
    monkeypatch.delenv("DEN_HERMES_POOL_MEMBER_ID", raising=False)
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    adapter = _adapter(gateway, channels)

    assert await adapter.connect() is True
    binding = gateway.bindings[-1]

    assert "pool_member_id" not in binding
    assert "agent_instance_id" not in binding
    assert binding["adapter_instance_id"] == "test-host:den-mcp-runner:runner:gateway"


@pytest.mark.asyncio
async def test_claim_payload_includes_pool_member_id_when_configured() -> None:
    """Claim payload must include pool_member_id for concrete slot targeting."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    adapter = _adapter_with_pool_member(gateway, channels)

    payload = adapter._claim_payload()

    assert payload["pool_member_id"] == "pool-coder-02"
    assert payload["agent_instance_id"] == "hermes:den-k8:spawned-coder:pool-coder-02:live"
    assert payload["adapter_instance_id"] == "hermes:den-k8:spawned-coder:pool-coder-02:live"
    assert payload["agent_identity"] == "spawned-coder"


@pytest.mark.asyncio
async def test_claim_payload_omits_pool_member_id_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Generic adapter claim must not include pool_member_id."""
    monkeypatch.delenv("DEN_HERMES_POOL_MEMBER_ID", raising=False)
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    adapter = _adapter(gateway, channels)

    payload = adapter._claim_payload()

    assert "pool_member_id" not in payload
    assert "agent_instance_id" not in payload


@pytest.mark.asyncio
async def test_pool_member_id_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """pool_member_id must be readable from DEN_HERMES_POOL_MEMBER_ID env."""
    monkeypatch.setenv("DEN_HERMES_POOL_MEMBER_ID", "pool-coder-03")
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    adapter = DenChannelsAdapter(
        PlatformConfig(
            enabled=True,
            token="***",
            extra={
                "gateway_url": "http://192.168.1.10:18080",
                "channels_url": "http://192.168.1.10:18080",
                "project_id": "den-hermes-bridge",
                "agent_identity": "spawned-coder",
                "role": "coder",
                "profile": "spawned-coder",
                "start_claim_loop": False,
                "token": "test-token",
            },
        ),
        gateway_client=gateway,
        channels_client=channels,
    )

    assert adapter.pool_member_id == "pool-coder-03"

    binding = adapter._binding_payload()
    assert binding["pool_member_id"] == "pool-coder-03"
    assert binding["agent_instance_id"].endswith(":spawned-coder:coder:gateway")

    claim = adapter._claim_payload()
    assert claim["pool_member_id"] == "pool-coder-03"
    assert claim["agent_instance_id"] == binding["agent_instance_id"]


@pytest.mark.asyncio
async def test_activity_environment_includes_pool_member_id() -> None:
    """Activity context must preserve poolMemberId for concrete slot targeting."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        120: {"id": 120, "channelId": 42, "senderIdentity": "runner", "body": "work please"},
    }
    adapter = _adapter_with_pool_member(gateway, channels)
    event = await adapter.delivery_to_event({
        **_delivery(801, 120, attempt_id=1001),
        "task_id": 1876,
    })

    await adapter.on_processing_start(event)
    context = _adapter_module._activity_context()

    assert context["poolMemberId"] == "pool-coder-02"
    assert context["agentInstanceId"] == "hermes:den-k8:spawned-coder:pool-coder-02:live"
    assert context["profileIdentity"] == "spawned-coder"
    assert context["taskId"] == 1876
    assert context["channelId"] == 42

    await adapter.on_processing_complete(event, _adapter_module.ProcessingOutcome.FAILURE)
    assert _adapter_module._activity_context() == {}


@pytest.mark.asyncio
async def test_activity_environment_omits_pool_member_id_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Activity context must not include poolMemberId when no pool member is configured."""
    monkeypatch.delenv("DEN_HERMES_POOL_MEMBER_ID", raising=False)
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        121: {"id": 121, "channelId": 42, "senderIdentity": "runner", "body": "generic work"},
    }
    adapter = _adapter(gateway, channels)
    event = await adapter.delivery_to_event(_delivery(802, 121, attempt_id=1002))

    await adapter.on_processing_start(event)
    context = _adapter_module._activity_context()

    assert "poolMemberId" not in context
    assert "agentInstanceId" not in context
    # profileIdentity should still be set
    assert context["profileIdentity"] == "den-mcp-runner"

    await adapter.on_processing_complete(event, _adapter_module.ProcessingOutcome.FAILURE)


@pytest.mark.asyncio
async def test_activity_environment_preserves_target_work_metadata() -> None:
    """Activity context must forward worker run/role/assignment from delivery metadata."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        122: {"id": 122, "channelId": 42, "senderIdentity": "runner", "body": "targeted work"},
    }
    adapter = _adapter(gateway, channels)
    event = await adapter.delivery_to_event({
        **_delivery(803, 122, attempt_id=1003),
        "task_id": 1876,
        "worker_run_id": "dc-1876-20260602-coder",
        "worker_role": "coder",
        "assignment_id": 99,
        "pool_member_id": "pool-coder-02",
        "agent_instance_id": "hermes:den-k8:spawned-coder:pool-coder-02:live",
    })

    await adapter.on_processing_start(event)
    context = _adapter_module._activity_context()

    assert context["poolMemberId"] == "pool-coder-02"
    assert context["agentInstanceId"] == "hermes:den-k8:spawned-coder:pool-coder-02:live"
    assert context["workerRunId"] == "dc-1876-20260602-coder"
    assert context["workerRole"] == "coder"
    assert context["assignmentId"] == 99
    assert context["taskId"] == 1876

    await adapter.on_processing_complete(event, _adapter_module.ProcessingOutcome.FAILURE)


@pytest.mark.asyncio
async def test_activity_environment_preserves_target_work_from_metadata_json() -> None:
    """Activity context must pick up worker metadata from nested metadata_json."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        123: {"id": 123, "channelId": 42, "senderIdentity": "runner", "body": "nested meta"},
    }
    adapter = _adapter(gateway, channels)
    event = await adapter.delivery_to_event({
        **_delivery(804, 123, attempt_id=1004),
        "metadata_json": json.dumps({
            "channel_id": 42,
            "channel_slug": "direct-agent-messages",
            "workerRunId": "dc-1876-meta-run",
            "workerRole": "reviewer",
            "poolMemberId": "pool-reviewer-01",
            "agentInstanceId": "hermes:den-k8:spawned-reviewer:pool-reviewer-01:live",
            "targetAssignmentId": 42,
        }),
    })

    await adapter.on_processing_start(event)
    context = _adapter_module._activity_context()

    assert context["poolMemberId"] == "pool-reviewer-01"
    assert context["agentInstanceId"] == "hermes:den-k8:spawned-reviewer:pool-reviewer-01:live"
    assert context["workerRunId"] == "dc-1876-meta-run"
    assert context["workerRole"] == "reviewer"
    assert context["assignmentId"] == 42

    await adapter.on_processing_complete(event, _adapter_module.ProcessingOutcome.FAILURE)


@pytest.mark.asyncio
async def test_pool_member_adapter_full_flow_end_to_end() -> None:
    """Pool-member-aware adapter must complete a full delivery -> send -> complete flow."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        130: {"id": 130, "channelId": 42, "senderIdentity": "runner", "body": "pool task"},
    }
    adapter = _adapter_with_pool_member(gateway, channels)

    assert await adapter.connect() is True

    # Verify binding
    binding = gateway.bindings[-1]
    assert binding["pool_member_id"] == "pool-coder-02"
    assert binding["agent_instance_id"] == "hermes:den-k8:spawned-coder:pool-coder-02:live"

    # Process a delivery
    event = await adapter.delivery_to_event({
        **_delivery(900, 130, attempt_id=1100),
        "task_id": 1876,
        "worker_run_id": "dc-1876-20260602-coder",
        "worker_role": "coder",
        "assignment_id": 99,
    })
    await adapter.on_processing_start(event)

    # Verify activity context has concrete identity
    activity = _adapter_module._activity_context()
    assert activity["poolMemberId"] == "pool-coder-02"
    assert activity["agentInstanceId"] == "hermes:den-k8:spawned-coder:pool-coder-02:live"
    assert activity["workerRunId"] == "dc-1876-20260602-coder"
    assert activity["workerRole"] == "coder"
    assert activity["assignmentId"] == 99

    # Send final reply
    result = await adapter.send(
        event.source.chat_id,
        "Pool-aware coder completed the task.",
        metadata={"delivery_request_id": 900, "notify": True},
    )

    assert result.success is True
    assert len(gateway.completed) == 1
    assert gateway.completed[0][0] == 900
    assert gateway.completed[0][1]["adapter_instance_id"] == "hermes:den-k8:spawned-coder:pool-coder-02:live"

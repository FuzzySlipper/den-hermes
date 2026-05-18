from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from gateway.config import PlatformConfig

_ADAPTER_PATH = Path(__file__).resolve().parents[1] / "plugins" / "platforms" / "den_channels" / "adapter.py"
_SPEC = importlib.util.spec_from_file_location("den_channels_adapter_under_test", _ADAPTER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_adapter_module = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _adapter_module
_SPEC.loader.exec_module(_adapter_module)
DenChannelsAdapter = _adapter_module.DenChannelsAdapter


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

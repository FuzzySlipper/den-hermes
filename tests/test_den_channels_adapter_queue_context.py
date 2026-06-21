from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
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
        self.membership_discovery: dict[str, Any] = {"memberships": []}
        self.membership_requests: list[dict[str, Any]] = []
        self.posts: list[tuple[str | int, dict[str, Any]]] = []
        self.reactions: list[tuple[str | int, dict[str, Any]]] = []
        self._next_post_id = 9000
        self.subscription_discovery: dict[str, Any] = {"subscriptions": []}
        self.subscription_requests: list[dict[str, Any]] = []
        self.subscription_cursors: dict[int, list[dict[str, Any]]] = {}
        self.cursor_requests: list[int] = []
        self.fail_cursor_list: list[int] | None = None  # subscription IDs to fail

    async def get_direct_agent_events(self, *, channel_id: int, after_id: int = 0, limit: int = 10) -> dict[str, Any]:
        self.event_requests.append((channel_id, after_id, limit))
        return dict(self.events_by_channel.get(channel_id, {"items": [], "nextAfterId": after_id}))

    async def get_channel_memberships(
        self,
        *,
        member_identity: str,
        include_left: bool = False,
        include_ordinary_memberships: bool = False,
        limit: int = 200,
    ) -> dict[str, Any]:
        self.membership_requests.append({
            "member_identity": member_identity,
            "include_left": include_left,
            "include_ordinary_memberships": include_ordinary_memberships,
            "limit": limit,
        })
        return dict(self.membership_discovery)

    async def get_message_readback(self, message_id: str | int) -> dict[str, Any]:
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

    async def get_subscriptions(
        self,
        *,
        member_identity: str,
        profile_identity: str | None = None,
        channel_id: int | None = None,
        subscription_purpose: str | None = None,
        include_inactive: bool = False,
        limit: int = 200,
    ) -> dict[str, Any]:
        self.subscription_requests.append({
            "member_identity": member_identity,
            "profile_identity": profile_identity,
            "channel_id": channel_id,
            "subscription_purpose": subscription_purpose,
            "include_inactive": include_inactive,
            "limit": limit,
        })
        return dict(self.subscription_discovery)

    async def upsert_subscription_cursor(
        self,
        *,
        subscription_id: int,
        stream_kind: str = "direct_agent_events",
        last_seen_id: int,
        cursor_json: str | None = None,
    ) -> dict[str, Any]:
        return {"ok": True}

    async def list_subscription_cursors(
        self,
        *,
        subscription_id: int,
    ) -> list[dict[str, Any]]:
        self.cursor_requests.append(subscription_id)
        if self.fail_cursor_list is not None and subscription_id in self.fail_cursor_list:
            raise RuntimeError(f"Simulated cursor failure for sub {subscription_id}")
        return list(self.subscription_cursors.get(subscription_id, []))


class FakeConversationClient:
    def __init__(self, channels: FakeChannelsClient, *, fail: bool = False) -> None:
        self.channels = channels
        self.fail = fail
        self.posts: list[tuple[str | int, dict[str, Any], str | None]] = []

    async def post_channel_message(
        self,
        channel_id: str | int,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
    ) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("simulated conversation successor failure")
        self.posts.append((channel_id, payload, dedupe_key))
        # Keep existing assertions readable by mirroring the old camelCase shape
        # in the fake channel post ledger. Production uses the snake_case payload.
        legacy_shape = {
            "senderType": payload.get("sender_type"),
            "senderIdentity": payload.get("sender_identity"),
            "body": payload.get("body"),
            "messageKind": payload.get("message_kind"),
            "sourceKind": payload.get("source_kind"),
            "sourceId": payload.get("source_id"),
            "sourceProjectId": payload.get("source_project_id"),
            "dedupeKey": payload.get("dedupe_key"),
            "metadataJson": payload.get("metadata"),
        }
        if "reply_to_message_id" in payload:
            legacy_shape["replyToMessageId"] = payload["reply_to_message_id"]
        if "thread_root_message_id" in payload:
            legacy_shape["threadRootMessageId"] = payload["thread_root_message_id"]
        self.channels.posts.append((channel_id, legacy_shape))
        self.channels._next_post_id += 1
        return {"id": self.channels._next_post_id}


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
        conversation_client=FakeConversationClient(channels),
    )


def _channels_only_adapter(channels: FakeChannelsClient) -> DenChannelsAdapter:
    return DenChannelsAdapter(
        PlatformConfig(
            enabled=True,
            token="***",
            extra={
                "channels_url": "http://192.168.1.10:18081",
                "project_id": "goblinbench",
                "agent_identity": "goblin-overseer",
                "role": "agent",
                "profile": "goblin-overseer",
                "adapter_instance_id": "test-host:goblin-overseer:agent:gateway",
                "start_claim_loop": False,
                "start_poll_loop": False,
            },
        ),
        channels_client=channels,
        conversation_client=FakeConversationClient(channels),
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
async def test_direct_agent_event_poller_filters_target_and_advances_cursor(tmp_path: Path) -> None:
    channels = FakeChannelsClient()
    channels.events_by_channel[672] = {
        "items": [
            {"id": 10, "channelId": 672, "memberIdentity": "someone-else", "body": "ignore"},
            {"id": 11, "channelId": 672, "memberIdentity": "den-mcp-runner", "body": "deliver"},
            {"id": 12, "channelId": 672, "sourceId": "direct-agent-message:672:den-mcp-runner:abc", "body": "also deliver"},
        ],
        "nextAfterId": 12,
    }
    old_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(tmp_path)
    try:
        poller = _adapter_module._DirectAgentEventPoller(channels, "den-mcp-runner")

        first = await poller.poll(672, limit=10)
        second = await poller.poll(672, limit=10)

        assert [event["id"] for event in first] == [11, 12]
        assert second == []
        assert channels.event_requests == [(672, 0, 10), (672, 12, 10)]
    finally:
        if old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old_home


@pytest.mark.asyncio
async def test_configured_poll_channel_ids_are_used_with_membership_discovery() -> None:
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
    assert channels.membership_requests == [{
        "member_identity": "spawned-coder",
        "include_left": False,
        "include_ordinary_memberships": True,
        "limit": 200,
    }]


@pytest.mark.asyncio
async def test_member_identity_membership_discovery_finds_worker_pool_and_target_channels() -> None:
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.membership_discovery = {
        "memberIdentity": "spawned-coder",
        "memberships": [
            {"channelId": 604, "channelSlug": "worker-pool", "membershipStatus": "active", "membershipPurpose": "worker_pool_control"},
            {"channelId": 1, "channelSlug": "project-agora-os", "membershipStatus": "active", "membershipPurpose": "target_work"},
            {"channelId": 642, "channelSlug": "project-pi-crew", "membershipStatus": "left", "membershipPurpose": "target_work"},
            {"channelId": "bad", "membershipStatus": "active"},
        ],
    }
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
                "start_claim_loop": False,
                "start_poll_loop": False,
            },
        ),
        gateway_client=gateway,
        channels_client=channels,
    )

    first = await adapter._resolve_poll_channels()
    second = await adapter._resolve_poll_channels()

    assert first == [1, 604]
    assert second == [1, 604]
    assert channels.membership_requests == [{
        "member_identity": "spawned-coder",
        "include_left": False,
        "include_ordinary_memberships": True,
        "limit": 200,
    }]


@pytest.mark.asyncio
async def test_member_identity_membership_discovery_refreshes_after_interval() -> None:
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.membership_discovery = {
        "memberIdentity": "spawned-reviewer",
        "memberships": [
            {"channelId": 604, "membershipStatus": "active", "membershipPurpose": "worker_pool_control"},
        ],
    }
    adapter = DenChannelsAdapter(
        PlatformConfig(
            enabled=True,
            token="***",
            extra={
                "channels_url": "http://192.168.1.10:18081",
                "project_id": "",
                "agent_identity": "spawned-reviewer",
                "role": "reviewer",
                "profile": "spawned-reviewer",
                "adapter_instance_id": "test-host:spawned-reviewer:reviewer:worker",
                "channel_discovery_interval_seconds": 30,
                "start_claim_loop": False,
                "start_poll_loop": False,
            },
        ),
        gateway_client=gateway,
        channels_client=channels,
    )

    first = await adapter._resolve_poll_channels()
    adapter._last_channel_discovery -= adapter._channel_discovery_interval + 1
    channels.membership_discovery = {
        "memberIdentity": "spawned-reviewer",
        "memberships": [
            {"channelId": 604, "membershipStatus": "active", "membershipPurpose": "worker_pool_control"},
            {"channelId": 1, "membershipStatus": "active", "membershipPurpose": "target_work"},
        ],
    }
    second = await adapter._resolve_poll_channels()

    assert first == [604]
    assert second == [1, 604]
    assert channels.membership_requests == [
        {
            "member_identity": "spawned-reviewer",
            "include_left": False,
            "include_ordinary_memberships": True,
            "limit": 200,
        },
        {
            "member_identity": "spawned-reviewer",
            "include_left": False,
            "include_ordinary_memberships": True,
            "limit": 200,
        },
    ]




@pytest.mark.asyncio
async def test_poll_scope_hybrids_static_ids_with_runtime_membership_discovery() -> None:
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.membership_discovery = {
        "memberIdentity": "den-mcp-runner",
        "memberships": [
            {"channelId": 697, "channelSlug": "project-den-memory", "membershipStatus": "active", "membershipPurpose": None},
            {"channelId": 698, "channelSlug": "old-project", "membershipStatus": "left", "membershipPurpose": None},
        ],
    }
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
                "poll_channel_ids": [672],
                "start_claim_loop": False,
                "start_poll_loop": False,
            },
        ),
        gateway_client=gateway,
        channels_client=channels,
    )

    channels_to_poll = await adapter._resolve_poll_channels()

    assert channels_to_poll == [672, 697]
    assert channels.membership_requests == [{
        "member_identity": "den-mcp-runner",
        "include_left": False,
        "include_ordinary_memberships": True,
        "limit": 200,
    }]




@pytest.mark.asyncio
async def test_membership_discovery_clears_removed_dynamic_channels_after_refresh() -> None:
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.membership_discovery = {
        "memberIdentity": "den-mcp-runner",
        "memberships": [
            {"channelId": 697, "channelSlug": "project-den-memory", "membershipStatus": "active", "membershipPurpose": None},
        ],
    }
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
                "channel_discovery_interval_seconds": 30,
                "start_claim_loop": False,
                "start_poll_loop": False,
            },
        ),
        gateway_client=gateway,
        channels_client=channels,
    )

    first = await adapter._resolve_poll_channels()
    adapter._last_channel_discovery -= adapter._channel_discovery_interval + 1
    channels.membership_discovery = {"memberIdentity": "den-mcp-runner", "memberships": []}
    second = await adapter._resolve_poll_channels()

    assert first == [697]
    assert second == []

@pytest.mark.asyncio
async def test_poll_initial_after_ids_seed_direct_event_cursors(tmp_path: Path) -> None:
    channels = FakeChannelsClient()
    channels.events_by_channel[697] = {
        "items": [
            {"id": 5411, "channelId": 697, "sourceId": "direct-agent-message:697:den-mcp-runner:old", "body": "old"},
            {"id": 5412, "channelId": 697, "sourceId": "direct-agent-message:697:den-mcp-runner:new", "body": "new"},
        ],
        "nextAfterId": 5412,
    }
    old_home = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = str(tmp_path)
    try:
        poller = _adapter_module._DirectAgentEventPoller(
            channels,
            "den-mcp-runner",
            initial_after_ids={697: 5411},
        )

        events = await poller.poll(697, limit=10)

        assert [event["id"] for event in events] == [5412]
        assert channels.event_requests == [(697, 5411, 10)]
    finally:
        if old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old_home


@pytest.mark.asyncio
async def test_direct_agent_event_poller_cursor_persists_across_restart(tmp_path: Path) -> None:
    """Simulate gateway restart: cursor file survives, old events not replayed."""
    import tempfile

    hermes_home = str(tmp_path)
    old_env = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = hermes_home
    try:
        channels = FakeChannelsClient()
        channels.events_by_channel[672] = {
            "items": [
                {"id": 10, "channelId": 672, "sourceId": "direct-agent-message:672:den-mcp-runner:a"},
                {"id": 11, "channelId": 672, "sourceId": "direct-agent-message:672:den-mcp-runner:b"},
                {"id": 12, "channelId": 672, "sourceId": "direct-agent-message:672:den-mcp-runner:c"},
            ],
            "nextAfterId": 12,
        }

        # First poller instance — processes events, advances cursor, persists.
        poller1 = _adapter_module._DirectAgentEventPoller(channels, "den-mcp-runner")
        first = await poller1.poll(672, limit=10)
        assert [e["id"] for e in first] == [10, 11, 12]
        assert channels.event_requests == [(672, 0, 10)]

        # Second poller instance — simulates restart, loads persisted cursors.
        poller2 = _adapter_module._DirectAgentEventPoller(channels, "den-mcp-runner")
        second = await poller2.poll(672, limit=10)

        # After restart, old events 10-12 should NOT be replayed.
        assert second == []
        assert channels.event_requests == [
            (672, 0, 10),  # first poller start
            (672, 12, 10),  # second poller start — cursor loaded from file
        ]
    finally:
        if old_env is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old_env


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
async def test_adapter_channel_reply_fails_closed_when_conversation_successor_fails() -> None:
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        106: {"id": 106, "channelId": 42, "senderIdentity": "patch", "body": "please reply"},
    }
    adapter = DenChannelsAdapter(
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
        conversation_client=FakeConversationClient(channels, fail=True),
    )
    event = await adapter.delivery_to_event(_delivery(606, 106, attempt_id=806))

    result = await adapter.send(
        event.source.chat_id,
        "this should fail closed",
        metadata={"delivery_request_id": 606, "notify": True},
    )

    assert result.success is False
    assert "simulated conversation successor failure" in (result.error or "")
    assert channels.posts == []
    assert gateway.completed == []
    assert gateway.failed
    assert gateway.failed[-1][0] == 606


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


@pytest.mark.asyncio
async def test_adapter_activity_hook_skips_when_only_channels_url_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adapter-level breadcrumb context must not derive Observation from channels_url."""
    monkeypatch.delenv("DEN_OBSERVATION_URL", raising=False)
    monkeypatch.delenv("DEN_OBSERVATION_TOKEN", raising=False)
    posted: list[tuple[str, dict[str, Any]]] = []

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

    channels = FakeChannelsClient()
    channels.messages = {
        130: {"id": 130, "channelId": 42, "senderIdentity": "runner", "body": "work"},
    }
    adapter = _channels_only_adapter(channels)
    event = await adapter.delivery_to_event(_delivery(901, 130, attempt_id=1101))
    context = adapter._build_context(event)
    assert context is not None
    adapter._set_activity_environment(context)
    try:
        activity_context = _adapter_module._activity_context()
        assert activity_context["channelsUrl"] == "http://192.168.1.10:18081"
        assert activity_context["observationUrl"] == ""
        _on_pre_tool_call(tool_name="terminal", args={"command": "date"})
    finally:
        adapter._clear_activity_environment(context)

    assert posted == []


@pytest.mark.asyncio
async def test_generic_adapter_activity_hook_uses_adapter_instance_for_agent_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generic non-pool breadcrumbs should still carry stable Observation agent identity."""
    monkeypatch.delenv("DEN_HERMES_AGENT_INSTANCE_ID", raising=False)
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

    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        131: {"id": 131, "channelId": 42, "senderIdentity": "runner", "body": "work"},
    }
    adapter = DenChannelsAdapter(
        PlatformConfig(
            enabled=True,
            token="test-token",
            extra={
                "gateway_url": "http://192.168.1.10:18080",
                "channels_url": "http://192.168.1.10:18081",
                "observation_url": "http://obs.test",
                "project_id": "den-hermes-bridge",
                "agent_identity": "den-mcp-runner",
                "role": "runner",
                "profile": "den-mcp-runner",
                "adapter_instance_id": "test-host:den-mcp-runner:runner:gateway",
                "start_claim_loop": False,
            },
        ),
        gateway_client=gateway,
        channels_client=channels,
        conversation_client=FakeConversationClient(channels),
    )
    event = await adapter.delivery_to_event(_delivery(902, 131, attempt_id=1102))
    context = adapter._build_context(event)
    assert context is not None
    adapter._set_activity_environment(context)
    try:
        activity_context = _adapter_module._activity_context()
        assert "agentInstanceId" not in activity_context
        assert activity_context["adapterInstanceId"] == "test-host:den-mcp-runner:runner:gateway"
        _on_pre_tool_call(tool_name="terminal", args={"command": "date"})
    finally:
        adapter._clear_activity_environment(context)

    assert posted[0]["agent_identity"] == {
        "profile": "den-mcp-runner",
        "instance_id": "test-host:den-mcp-runner:runner:gateway",
    }


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
            "observationUrl": "http://obs.test",
            "channelId": 42,
            "projectId": "den-hermes-bridge",
            "agentIdentity": "den-mcp-runner",
            "deliveryRequestId": 701,
            "hermesSessionKey": "project:den-hermes-bridge:channel:42",
        },
        normalize_tool_activity("terminal", {"command": "date"}),
    )

    assert posted[0]["payload"]["session_key"] == "project:den-hermes-bridge:channel:42"


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
            "observationUrl": "http://obs.test",
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
    activity = event["payload"]
    assert activity["work_ref"]["run_id"] == "coder-run-1"
    metadata = activity["metadata"]
    assert metadata["displayBlockId"] == "block-701"
    assert metadata["parentHermesSessionKey"] == "parent-session"
    assert metadata["parentAgentIdentity"] == "den-mcp-runner"
    assert metadata["workerRunId"] == "coder-run-1"
    assert metadata["workerRole"] == "coder"


def test_activity_emitter_uses_observation_successor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Activity emission writes successor Observation events, not legacy Channels events."""
    posted: list[tuple[str, dict[str, Any]]] = []

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
            "observation_url": "http://obs.test",
            "channelId": 42,
            "projectId": "den-hermes-bridge",
            "agentIdentity": "den-mcp-runner",
            "agentInstanceId": "hermes:test:runner",
            "deliveryRequestId": 702,
        },
        normalize_tool_activity("terminal", {"command": "date"}),
    )

    assert len(posted) == 1
    url, body = posted[0]
    assert url == "http://obs.test/v1/observation/activity-events"
    assert body["source_domain"] == "runtime"
    assert body["event_type"] == "tool_call_started"
    assert body["agent_identity"] == {"profile": "den-mcp-runner", "instance_id": "hermes:test:runner"}
    assert body["payload"]["kind"] == "agent_activity.v1"
    assert body["payload"]["tool_name"] == "terminal"
    assert body["payload"]["work_ref"]["channel_id"] == 42


def test_activity_emitter_prefers_observation_over_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    """When both observationUrl and channelsUrl are set, only observationUrl is used."""
    posted: list[tuple[str, dict[str, Any]]] = []

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
            "observationUrl": "http://obs.test",
            "channelsUrl": "http://channels.test",
            "channelId": 42,
            "projectId": "den-hermes-bridge",
            "agentIdentity": "den-mcp-runner",
        },
        normalize_tool_activity("terminal", {"command": "date"}),
    )

    assert len(posted) == 1
    url, _body = posted[0]
    assert url == "http://obs.test/v1/observation/activity-events"
    assert "channels.test" not in url


def test_activity_emitter_does_not_fallback_to_channels_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """When only channelsUrl is set, legacy activity emission is skipped."""
    posted: list[tuple[str, dict[str, Any]]] = []

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
            "channelId": 42,
            "projectId": "den-hermes-bridge",
            "agentIdentity": "den-mcp-runner",
        },
        normalize_tool_activity("terminal", {"command": "date"}),
    )

    assert posted == []


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
            "observationUrl": "http://obs.test",
            "channelId": 42,
            "projectId": "den-hermes-bridge",
            "taskId": 1567,
            "threadId": 9001,
            "agentIdentity": "den-coder-profile",
            "agentInstanceId": "hermes:test:coder",
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
    assert event["source_domain"] == "runtime"
    assert event["event_type"] == "tool_call_started"
    assert event["agent_identity"] == {"profile": "den-coder-profile", "instance_id": "hermes:test:coder"}
    activity = event["payload"]
    assert activity["kind"] == "agent_activity.v1"
    assert activity["schema_version"] == 1
    assert activity["work_ref"]["project_id"] == "den-hermes-bridge"
    assert activity["work_ref"]["task_id"] == 1567
    assert activity["work_ref"]["channel_id"] == 42
    assert activity["work_ref"]["run_id"] == "coder-1567"
    metadata = activity["metadata"]
    assert metadata["displayBlockId"] == "parent-1567"
    assert metadata["parentHermesSessionKey"] == "parent-session-1567"
    assert metadata["parentAgentIdentity"] == "den-mcp-runner"
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
        "channelsUrl": "http://gateway.test",
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
            "channelsUrl": "http://gateway.test",
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
        "channelsUrl": "http://gateway.test",
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
        "channelsUrl": "http://gateway.test",
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
async def test_channels_only_direct_event_final_reply_does_not_warn_when_gateway_client_absent(caplog: pytest.LogCaptureFixture) -> None:
    """Direct-agent polling uses Channels evidence, not legacy Gateway completion endpoints."""
    channels = FakeChannelsClient()
    adapter = _channels_only_adapter(channels)
    delivery = adapter._event_to_delivery({
        "id": 2395,
        "channelId": 601,
        "sourceProjectId": "goblinbench",
        "sourceKind": "wake_event",
        "sourceId": "direct-agent-message:601:goblin-overseer:abc",
        "senderIdentity": "Patch",
        "summary": "Direct agent request to goblin-overseer: recorded, pending claim/completion",
        "body": "smoke direct reply completion bookkeeping",
        "metadataJson": json.dumps({"channel_slug": "project-goblinbench"}),
    })
    event = await adapter.delivery_to_event(delivery)
    await adapter.on_processing_start(event)

    with caplog.at_level(logging.WARNING):
        result = await adapter.send(
            event.source.chat_id,
            "Visible final reply.",
            metadata={"delivery_request_id": 2395, "notify": True},
        )

    assert result.success is True
    assert channels.posts
    posted_channel_id, posted_payload = channels.posts[-1]
    assert posted_channel_id == 601
    assert posted_payload["sourceKind"] == "gateway_delivery"
    assert posted_payload["sourceId"] == "2395"
    assert "failed to mark completed" not in caplog.text


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
    assert parsed.get("error") == "DEN_DELIVERY_URL or DEN_GATEWAY_URL is not configured"


def test_direct_agent_message_handler_available_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """check_fn must account for env and adapter-config defaults."""
    monkeypatch.delenv("DEN_CHANNELS_URL", raising=False)
    monkeypatch.delenv("DEN_GATEWAY_URL", raising=False)
    _adapter_module._DIRECT_AGENT_CONFIG_DEFAULTS.clear()
    assert _adapter_module._check_direct_agent_message_available() is False
    monkeypatch.setenv("DEN_DELIVERY_URL", "http://test:8080")
    assert _adapter_module._check_direct_agent_message_available() is True
    monkeypatch.delenv("DEN_DELIVERY_URL", raising=False)
    _adapter_module._remember_direct_agent_config(delivery_url="http://profile-config.test")
    assert _adapter_module._check_direct_agent_message_available() is True




def test_direct_agent_message_handler_uses_adapter_config_and_defaults_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct-agent tool must work from platform config defaults, not prompt-level shell env."""
    monkeypatch.delenv("DEN_CHANNELS_URL", raising=False)
    monkeypatch.delenv("DEN_GATEWAY_URL", raising=False)
    monkeypatch.delenv("DEN_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("DEN_CHANNELS_TOKEN", raising=False)
    _adapter_module._DIRECT_AGENT_CONFIG_DEFAULTS.clear()
    _adapter_module._remember_direct_agent_config(
        delivery_url="http://channels.test",
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
    assert captured["url"] == "http://channels.test/v1/delivery/intents"
    assert captured["json"]["target_identity"] == {"profile": "reviewer", "instance_id": "reviewer@unknown"}
    assert captured["json"]["idempotency_key"].startswith("wake:ch569:reviewer:")
    assert captured["json"]["source_ref"].startswith("wake://reviewer?body=")
    assert captured["json"]["ttl_seconds"] == 300
    assert captured["json"]["channel_id"] == 569


def test_direct_agent_message_handler_accepts_registry_args_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermes registry dispatch passes the tool argument dict positionally."""
    monkeypatch.delenv("DEN_CHANNELS_URL", raising=False)
    monkeypatch.delenv("DEN_GATEWAY_URL", raising=False)
    monkeypatch.delenv("DEN_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("DEN_CHANNELS_TOKEN", raising=False)
    _adapter_module._DIRECT_AGENT_CONFIG_DEFAULTS.clear()
    _adapter_module._remember_direct_agent_config(
        delivery_url="http://channels.test",
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
    assert captured["url"] == "http://channels.test/v1/delivery/intents"
    assert captured["json"]["target_identity"] == {"profile": "reviewer", "instance_id": "reviewer@unknown"}
    assert captured["json"]["channel_id"] == 569


def test_direct_agent_message_handler_forwards_explicit_worker_selectors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Worker/reviewer wakes must use logical member identity plus concrete selectors."""
    monkeypatch.delenv("DEN_CHANNELS_URL", raising=False)
    monkeypatch.delenv("DEN_GATEWAY_URL", raising=False)
    monkeypatch.delenv("DEN_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("DEN_CHANNELS_TOKEN", raising=False)
    _adapter_module._DIRECT_AGENT_CONFIG_DEFAULTS.clear()
    _adapter_module._remember_direct_agent_config(
        delivery_url="http://channels.test",
        agent_identity="pi-crew-runner",
    )

    captured: dict[str, Any] = {}

    class FakeResponse:
        content = b"{}"

        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, Any]:
            return {"status": "recorded", "messageId": 789}

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
        "channel_id": 604,
        "member_identity": "spawned-reviewer",
        "body": "review task 2528",
        "target_task_id": 2528,
        "assignment_id": "1375",
        "worker_run_id": "piw-review-1375",
        "worker_role": "reviewer",
        "profile_identity": "spawned-reviewer",
        "pool_member_id": "pool-reviewer-03",
        "agent_instance_id": "hermes:den-k8:spawned-reviewer:pool-reviewer-03:live",
    }))
    parsed = json.loads(result)

    assert parsed["status"] == "ok"
    assert captured["url"] == "http://channels.test/v1/delivery/intents"
    json_payload = captured["json"]
    assert json_payload["target_identity"] == {
        "profile": "spawned-reviewer",
        "instance_id": "hermes:den-k8:spawned-reviewer:pool-reviewer-03:live",
    }
    assert json_payload["idempotency_key"].startswith("wake:ch604:spawned-reviewer:piw-review-1375")
    assert json_payload["source_ref"].startswith("wake://spawned-reviewer?body=")
    assert json_payload["ttl_seconds"] == 300
    assert json_payload["channel_id"] == 604
    assert json_payload["target_task_id"] == 2528
    assert json_payload["assignment_id"] == "1375"
    assert json_payload["worker_run_id"] == "piw-review-1375"
    assert json_payload["worker_role"] == "reviewer"
    assert json_payload["profile_identity"] == "spawned-reviewer"
    assert json_payload["pool_member_id"] == "pool-reviewer-03"
    assert json_payload["agent_instance_id"] == "hermes:den-k8:spawned-reviewer:pool-reviewer-03:live"


def test_direct_agent_message_tool_schema_exposes_worker_selector_fields() -> None:
    """The tool schema must let agents pass concrete selectors without abusing member_identity."""
    properties = _adapter_module._DIRECT_AGENT_MESSAGE_SCHEMA["parameters"]["properties"]

    assert properties["member_identity"]["description"].startswith("Logical active Channels member identity")
    assert "pool_member_id" in properties
    assert "agent_instance_id" in properties
    assert "worker_run_id" in properties
    assert "profile_identity" in properties


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
        conversation_client=FakeConversationClient(channels),
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
    # Generic profiles still expose adapterInstanceId as stable Observation attribution.
    assert context["adapterInstanceId"] == "test-host:den-mcp-runner:runner:gateway"
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


# =============================================================================
# Subscription discovery adapter tests (task #2554)
# =============================================================================


@pytest.mark.asyncio
async def test_subscription_discovery_without_static_config() -> None:
    """Adapter discovers active subscriptions and uses them as poll channels."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.subscription_discovery = {
        "memberIdentity": "den-mcp-runner",
        "subscriptions": [
            {"id": 301, "channelId": 111, "subscriptionStatus": "active", "subscriptionPurpose": "ordinary_channel"},
            {"id": 302, "channelId": 222, "subscriptionStatus": "active", "subscriptionPurpose": "ordinary_channel"},
        ],
    }
    adapter = DenChannelsAdapter(
        PlatformConfig(
            enabled=True, token="***",
            extra={
                "channels_url": "http://192.168.1.10:18081",
                "agent_identity": "den-mcp-runner",
                "role": "runner",
                "profile": "den-mcp-runner",
                "adapter_instance_id": "test-host:den-mcp-runner:runner:gateway",
                "start_claim_loop": False,
                "start_poll_loop": False,
            },
        ),
        gateway_client=gateway,
        channels_client=channels,
    )

    # Run subscription discovery
    assert not adapter._subscription_discovery_ever_ran
    await adapter._discover_and_sync_subscriptions()
    assert adapter._subscription_discovery_ever_ran
    assert adapter._subscription_cache == {111: 301, 222: 302}

    # Resolve poll channels — should return subscription-derived channels
    channels_list = await adapter._resolve_poll_channels()
    assert sorted(channels_list) == [111, 222]


@pytest.mark.asyncio
async def test_subscription_discovery_active_to_reduced_removes_channel() -> None:
    """When subscriptions reduce, the removed channel stops being polled."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.subscription_discovery = {
        "memberIdentity": "den-mcp-runner",
        "subscriptions": [
            {"id": 301, "channelId": 111, "subscriptionStatus": "active", "subscriptionPurpose": "ordinary_channel"},
            {"id": 302, "channelId": 222, "subscriptionStatus": "active", "subscriptionPurpose": "ordinary_channel"},
        ],
    }
    adapter = DenChannelsAdapter(
        PlatformConfig(
            enabled=True, token="***",
            extra={
                "channels_url": "http://192.168.1.10:18081",
                "agent_identity": "den-mcp-runner",
                "role": "runner",
                "profile": "den-mcp-runner",
                "adapter_instance_id": "test-host:den-mcp-runner:runner:gateway",
                "start_claim_loop": False,
                "start_poll_loop": False,
            },
        ),
        gateway_client=gateway,
        channels_client=channels,
    )

    # First discovery: both channels
    await adapter._discover_and_sync_subscriptions()
    assert adapter._subscription_cache == {111: 301, 222: 302}
    first_channels = await adapter._resolve_poll_channels()
    assert sorted(first_channels) == [111, 222]

    # Second discovery: only channel 222 remains
    # Expire the discovery interval so next call re-discovers
    adapter._last_subscription_discovery = 0.0
    channels.subscription_discovery = {
        "memberIdentity": "den-mcp-runner",
        "subscriptions": [
            {"id": 302, "channelId": 222, "subscriptionStatus": "active", "subscriptionPurpose": "ordinary_channel"},
        ],
    }

    await adapter._discover_and_sync_subscriptions()
    assert adapter._subscription_cache == {222: 302}
    removed_channels = await adapter._resolve_poll_channels()
    assert sorted(removed_channels) == [222]

    # Verify poller mapping for channel 111 was removed
    assert 111 not in adapter._event_poller._subscription_ids


@pytest.mark.asyncio
async def test_subscription_discovery_zero_subscriptions_suppresses_membership() -> None:
    """When subscriptions go to zero, membership fallback does not re-add channels."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    # Membership reports channel 111 as active
    channels.membership_discovery = {
        "memberIdentity": "den-mcp-runner",
        "memberships": [
            {"channelId": 111, "channelSlug": "project-test", "membershipStatus": "active", "membershipPurpose": None},
        ],
    }
    # Initially: active subscription for 111
    channels.subscription_discovery = {
        "memberIdentity": "den-mcp-runner",
        "subscriptions": [
            {"id": 301, "channelId": 111, "subscriptionStatus": "active", "subscriptionPurpose": "ordinary_channel"},
        ],
    }
    adapter = DenChannelsAdapter(
        PlatformConfig(
            enabled=True, token="***",
            extra={
                "channels_url": "http://192.168.1.10:18081",
                "agent_identity": "den-mcp-runner",
                "role": "runner",
                "profile": "den-mcp-runner",
                "adapter_instance_id": "test-host:den-mcp-runner:runner:gateway",
                "start_claim_loop": False,
                "start_poll_loop": False,
            },
        ),
        gateway_client=gateway,
        channels_client=channels,
    )

    # Run subscription discovery — 111 active
    await adapter._discover_and_sync_subscriptions()
    first = await adapter._resolve_poll_channels()
    assert sorted(first) == [111]

    # Now subscriptions go to zero
    adapter._last_subscription_discovery = 0.0
    channels.subscription_discovery = {
        "memberIdentity": "den-mcp-runner",
        "subscriptions": [],
    }

    await adapter._discover_and_sync_subscriptions()
    # Subscription cache is empty
    assert adapter._subscription_cache == {}
    # Membership is still active but subscription discovery has run,
    # so membership fallback should be suppressed.
    second = await adapter._resolve_poll_channels()
    assert sorted(second) == [], f"Expected no channels, got {second}"


@pytest.mark.asyncio
async def test_subscription_discovery_static_config_remains_fallback() -> None:
    """Static poll_channel_ids remain polled even when subscriptions are empty."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.subscription_discovery = {
        "memberIdentity": "den-mcp-runner",
        "subscriptions": [],
    }
    adapter = DenChannelsAdapter(
        PlatformConfig(
            enabled=True, token="***",
            extra={
                "channels_url": "http://192.168.1.10:18081",
                "agent_identity": "den-mcp-runner",
                "role": "runner",
                "profile": "den-mcp-runner",
                "adapter_instance_id": "test-host:den-mcp-runner:runner:gateway",
                "poll_channel_ids": [999],
                "start_claim_loop": False,
                "start_poll_loop": False,
            },
        ),
        gateway_client=gateway,
        channels_client=channels,
    )

    assert adapter.poll_channel_ids == [999]

    # Subscription discovery returns zero
    await adapter._discover_and_sync_subscriptions()
    assert adapter._subscription_cache == {}

    # Static config channel 999 should still be polled
    channels_list = await adapter._resolve_poll_channels()
    assert sorted(channels_list) == [999]


@pytest.mark.asyncio
async def test_subscription_discovery_cursor_init_failure_degrades_gracefully() -> None:
    """Cursor init failure from server should not crash discovery or poll loop."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.subscription_discovery = {
        "memberIdentity": "den-mcp-runner",
        "subscriptions": [
            {"id": 301, "channelId": 111, "subscriptionStatus": "active", "subscriptionPurpose": "ordinary_channel"},
        ],
    }
    # Simulate cursor list failure for subscription 301
    channels.fail_cursor_list = [301]
    adapter = DenChannelsAdapter(
        PlatformConfig(
            enabled=True, token="***",
            extra={
                "channels_url": "http://192.168.1.10:18081",
                "agent_identity": "den-mcp-runner",
                "role": "runner",
                "profile": "den-mcp-runner",
                "adapter_instance_id": "test-host:den-mcp-runner:runner:gateway",
                "start_claim_loop": False,
                "start_poll_loop": False,
            },
        ),
        gateway_client=gateway,
        channels_client=channels,
    )

    # Discovery should not raise despite cursor failure
    await adapter._discover_and_sync_subscriptions()
    assert adapter._subscription_cache == {111: 301}

    # Poll channels should still work
    channels_list = await adapter._resolve_poll_channels()
    assert sorted(channels_list) == [111]

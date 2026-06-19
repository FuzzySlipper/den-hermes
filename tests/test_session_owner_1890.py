"""Tests for #1890: agent-instance-global session ownership.

Session ownership must be keyed by the concrete agent instance, not by
source channel.  Source channel/thread/project/task/control-room lanes
are source/UI/reply-routing metadata, not transcript ownership.

Acceptance criteria covered:
- Same concrete durable agent instance from two different source channels
  resolves to the same Hermes session key while retaining different
  raw_chat_id/source metadata.
- Two concrete worker instances sharing one profile resolve to different
  session owners / session keys.
- assignment_run / fresh worker scope is distinct per assignment/run and
  does not share transcript context.
- Explicit source_lane / conversationLaneId remains an opt-in compatibility
  scope and is labeled as such.
- Activity / reply context still forwards source and target-work metadata.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from gateway.config import PlatformConfig
from gateway.session import SessionSource, build_session_key
from gateway.platform_registry import PlatformEntry, platform_registry

_ADAPTER_PATH = Path(__file__).resolve().parents[1] / "plugins" / "platforms" / "den_channels" / "adapter.py"
_SPEC = importlib.util.spec_from_file_location("den_channels_adapter_1890", _ADAPTER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_adapter_module = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _adapter_module
_SPEC.loader.exec_module(_adapter_module)
DenChannelsAdapter = _adapter_module.DenChannelsAdapter
_resolve_session_owner = _adapter_module._resolve_session_owner
_resolve_conversation_lane = _adapter_module._resolve_conversation_lane

platform_registry.register(PlatformEntry(
    name="den_channels",
    label="Den Channels",
    adapter_factory=lambda cfg: None,
    check_fn=lambda: True,
))


class FakeGatewayClient:
    def __init__(self) -> None:
        self.bindings: list[dict[str, Any]] = []
        self.completed: list[tuple[int, dict[str, Any]]] = []
        self.failed: list[tuple[int, dict[str, Any]]] = []

    async def upsert_adapter_binding(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.bindings.append(payload)
        return {"ok": True}

    async def claim_deliveries(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        return []

    async def mark_completed(self, delivery_request_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self.completed.append((delivery_request_id, payload))
        return {"ok": True}

    async def mark_failed(self, delivery_request_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self.failed.append((delivery_request_id, payload))
        return {"ok": True}


class FakeChannelsClient:
    def __init__(self) -> None:
        self.messages: dict[int, dict[str, Any]] = {}
        self.posts: list[tuple[str | int, dict[str, Any]]] = []
        self._next_post_id = 9000

    async def get_message_readback(self, message_id: str | int) -> dict[str, Any]:
        return dict(self.messages[int(message_id)])

    async def post_channel_message(self, channel_id: str | int, payload: dict[str, Any]) -> dict[str, Any]:
        self.posts.append((channel_id, payload))
        self._next_post_id += 1
        return {"id": self._next_post_id}


def _durable_adapter(
    gateway: FakeGatewayClient,
    channels: FakeChannelsClient,
    *,
    agent_instance_id: str = "hermes:den-k8:den-mcp-runner:runner-main:live",
) -> DenChannelsAdapter:
    """Build a durable-agent adapter with a concrete agent instance id."""
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
                "adapter_instance_id": agent_instance_id,
                "agent_instance_id": agent_instance_id,
                "start_claim_loop": False,
                "token": "test-token",
            },
        ),
        gateway_client=gateway,
        channels_client=channels,
    )


def _pool_adapter(
    gateway: FakeGatewayClient,
    channels: FakeChannelsClient,
    *,
    pool_member_id: str,
    agent_instance_id: str,
    profile: str = "spawned-coder",
) -> DenChannelsAdapter:
    """Build a worker pool adapter with concrete pool member identity."""
    return DenChannelsAdapter(
        PlatformConfig(
            enabled=True,
            token="***",
            extra={
                "gateway_url": "http://192.168.1.10:18080",
                "channels_url": "http://192.168.1.10:18080",
                "project_id": "den-hermes-bridge",
                "agent_identity": profile,
                "role": "coder",
                "profile": profile,
                "adapter_instance_id": agent_instance_id,
                "agent_instance_id": agent_instance_id,
                "pool_member_id": pool_member_id,
                "start_claim_loop": False,
                "token": "test-token",
            },
        ),
        gateway_client=gateway,
        channels_client=channels,
    )


def _delivery(
    delivery_id: int,
    message_id: int,
    *,
    attempt_id: int,
    channel_id: int = 42,
    project_id: str = "den-hermes-bridge",
    session_id: str = "session-42",
    extra_metadata: dict[str, Any] | None = None,
    extra_delivery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"channel_id": channel_id, "channel_slug": "ops-hub"}
    if extra_metadata:
        metadata.update(extra_metadata)
    result: dict[str, Any] = {
        "delivery_request_id": delivery_id,
        "attempt_id": attempt_id,
        "session_id": session_id,
        "project_id": project_id,
        "source_kind": "channel_message",
        "source_id": str(message_id),
        "metadata_json": json.dumps(metadata),
    }
    if extra_delivery:
        result.update(extra_delivery)
    return result


# ---------------------------------------------------------------------------
# 1. Same concrete durable agent instance from two source channels -> same session
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_same_agent_instance_same_session_across_source_channels() -> None:
    """A concrete durable agent addressed from two channels shares one session."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        100: {"id": 100, "channelId": 10, "senderIdentity": "ops", "body": "from channel 10"},
        101: {"id": 101, "channelId": 20, "senderIdentity": "ops", "body": "from channel 20"},
    }
    adapter = _durable_adapter(gateway, channels)

    event_a = await adapter.delivery_to_event(_delivery(501, 100, attempt_id=701, channel_id=10))
    event_b = await adapter.delivery_to_event(_delivery(502, 101, attempt_id=702, channel_id=20))

    # Same session owner -> same chat_id -> same session key
    key_a = build_session_key(event_a.source, group_sessions_per_user=False)
    key_b = build_session_key(event_b.source, group_sessions_per_user=False)
    assert key_a == key_b, (
        f"Same agent instance must produce same session key across channels: "
        f"{key_a!r} != {key_b!r}"
    )

    # Source channel metadata is preserved
    assert event_a.raw_message["raw_chat_id"] == "project:den-hermes-bridge:channel:10"
    assert event_b.raw_message["raw_chat_id"] == "project:den-hermes-bridge:channel:20"
    assert event_a.raw_message["channel_id"] == 10
    assert event_b.raw_message["channel_id"] == 20


@pytest.mark.asyncio
async def test_adapter_instance_id_is_concrete_durable_owner_fallback() -> None:
    """Durable gateways still get instance-global sessions before payload fields arrive."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        110: {"id": 110, "channelId": 10, "senderIdentity": "ops", "body": "first"},
        111: {"id": 111, "channelId": 99, "senderIdentity": "ops", "body": "second"},
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
                "adapter_instance_id": "hermes:den-k8:den-mcp-runner:runner:gateway",
                "start_claim_loop": False,
                "token": "test-token",
            },
        ),
        gateway_client=gateway,
        channels_client=channels,
    )

    event_a = await adapter.delivery_to_event(_delivery(511, 110, attempt_id=711, channel_id=10))
    event_b = await adapter.delivery_to_event(_delivery(512, 111, attempt_id=712, channel_id=99))

    assert event_a.source.chat_id == "owner:hermes:den-k8:den-mcp-runner:runner:gateway"
    assert event_a.source.chat_id == event_b.source.chat_id
    assert event_a.raw_message["raw_chat_id"] == "project:den-hermes-bridge:channel:10"
    assert event_b.raw_message["raw_chat_id"] == "project:den-hermes-bridge:channel:99"


# ---------------------------------------------------------------------------
# 2. Two worker instances sharing one profile -> different session owners
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_two_pool_workers_same_profile_get_different_sessions() -> None:
    """Two pool workers sharing a profile config must not share a session."""
    channels = FakeChannelsClient()
    channels.messages = {
        200: {"id": 200, "channelId": 7, "senderIdentity": "runner", "body": "work for coder-01"},
        201: {"id": 201, "channelId": 7, "senderIdentity": "runner", "body": "work for coder-02"},
    }

    adapter_a = _pool_adapter(
        FakeGatewayClient(), channels,
        pool_member_id="pool-coder-01",
        agent_instance_id="hermes:den-k8:spawned-coder:pool-coder-01:live",
    )
    adapter_b = _pool_adapter(
        FakeGatewayClient(), channels,
        pool_member_id="pool-coder-02",
        agent_instance_id="hermes:den-k8:spawned-coder:pool-coder-02:live",
    )

    event_a = await adapter_a.delivery_to_event(_delivery(601, 200, attempt_id=801, channel_id=7))
    event_b = await adapter_b.delivery_to_event(_delivery(602, 201, attempt_id=802, channel_id=7))

    key_a = build_session_key(event_a.source, group_sessions_per_user=False)
    key_b = build_session_key(event_b.source, group_sessions_per_user=False)
    assert key_a != key_b, (
        f"Pool workers sharing a profile must not share session: "
        f"{key_a!r} == {key_b!r}"
    )
    assert "pool-coder-01" in key_a
    assert "pool-coder-02" in key_b


# ---------------------------------------------------------------------------
# 3. assignment_run scope is distinct per assignment/run
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_assignment_run_scope_is_distinct_per_assignment() -> None:
    """Deliveries with different assignment_ids produce different sessions."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        300: {"id": 300, "channelId": 7, "senderIdentity": "runner", "body": "assignment 50"},
        301: {"id": 301, "channelId": 7, "senderIdentity": "runner", "body": "assignment 51"},
    }
    adapter = _pool_adapter(
        gateway, channels,
        pool_member_id="pool-coder-01",
        agent_instance_id="hermes:den-k8:spawned-coder:pool-coder-01:live",
    )

    event_a = await adapter.delivery_to_event(
        _delivery(701, 300, attempt_id=901, channel_id=7,
                  extra_metadata={"target_assignment_id": 50})
    )
    event_b = await adapter.delivery_to_event(
        _delivery(702, 301, attempt_id=902, channel_id=7,
                  extra_metadata={"target_assignment_id": 51})
    )

    key_a = build_session_key(event_a.source, group_sessions_per_user=False)
    key_b = build_session_key(event_b.source, group_sessions_per_user=False)
    assert key_a != key_b, (
        f"Assignment-scoped sessions must be distinct: {key_a!r} == {key_b!r}"
    )


@pytest.mark.asyncio
async def test_worker_run_scope_is_distinct_per_run() -> None:
    """Deliveries with different worker_run_ids produce different sessions."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        400: {"id": 400, "channelId": 7, "senderIdentity": "runner", "body": "run a"},
        401: {"id": 401, "channelId": 7, "senderIdentity": "runner", "body": "run b"},
    }
    adapter = _pool_adapter(
        gateway, channels,
        pool_member_id="pool-coder-01",
        agent_instance_id="hermes:den-k8:spawned-coder:pool-coder-01:live",
    )

    event_a = await adapter.delivery_to_event(
        _delivery(801, 400, attempt_id=1001, channel_id=7,
                  extra_metadata={"worker_run_id": "run-001"})
    )
    event_b = await adapter.delivery_to_event(
        _delivery(802, 401, attempt_id=1002, channel_id=7,
                  extra_metadata={"worker_run_id": "run-002"})
    )

    key_a = build_session_key(event_a.source, group_sessions_per_user=False)
    key_b = build_session_key(event_b.source, group_sessions_per_user=False)
    assert key_a != key_b, (
        f"Run-scoped sessions must be distinct: {key_a!r} == {key_b!r}"
    )


# ---------------------------------------------------------------------------
# 4. source_lane / conversationLaneId is opt-in compatibility scope
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_source_lane_scope_opt_in_bypasses_session_owner() -> None:
    """Explicit session_scope=source_lane forces channel-lane session identity."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        500: {"id": 500, "channelId": 42, "senderIdentity": "ops", "body": "source lane request"},
    }
    adapter = _durable_adapter(gateway, channels)

    # Without source_lane, the session owner (agent instance) dominates
    event_owner = await adapter.delivery_to_event(_delivery(901, 500, attempt_id=1101))
    assert event_owner.source.chat_id.startswith("owner:")

    # With source_lane, channel identity is used instead
    event_lane = await adapter.delivery_to_event(
        _delivery(902, 500, attempt_id=1102,
                  extra_metadata={"session_scope": "source_lane"})
    )
    assert not event_lane.source.chat_id.startswith("owner:"), (
        f"source_lane scope must bypass session owner: {event_lane.source.chat_id!r}"
    )


@pytest.mark.asyncio
async def test_conversation_lane_id_remains_opt_in_for_source_lane_scope() -> None:
    """conversationLaneId remains available when source_lane is explicit."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        510: {"id": 510, "channelId": 42, "senderIdentity": "ops", "body": "lane test"},
    }
    # Adapter without agent_instance_id or pool_member_id
    adapter = DenChannelsAdapter(
        PlatformConfig(
            enabled=True,
            token="***",
            extra={
                "gateway_url": "http://192.168.1.10:18080",
                "channels_url": "http://192.168.1.10:18080",
                "project_id": "den-hermes-bridge",
                "agent_identity": "generic-agent",
                "role": "agent",
                "profile": "generic-agent",
                "adapter_instance_id": "generic:adapter",
                "start_claim_loop": False,
                "token": "test-token",
            },
        ),
        gateway_client=gateway,
        channels_client=channels,
    )

    event = await adapter.delivery_to_event(
        _delivery(911, 510, attempt_id=1111,
                  extra_metadata={
                      "conversationLaneId": "my-explicit-lane",
                      "session_scope": "source_lane",
                  })
    )
    # Explicit source_lane scope uses the lane identity even when the adapter
    # has a concrete durable owner available.
    assert event.source.chat_id == "lane:my-explicit-lane"


# ---------------------------------------------------------------------------
# 5. Activity/reply context forwards source and target-work metadata
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_owner_context_preserves_source_metadata() -> None:
    """Session-owner-scoped events still carry source channel metadata."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        600: {"id": 600, "channelId": 42, "senderIdentity": "ops", "body": "check"},
    }
    adapter = _durable_adapter(gateway, channels)

    event = await adapter.delivery_to_event(
        _delivery(1001, 600, attempt_id=1201,
                  extra_metadata={"target_task_id": 1890})
    )

    # Session owner identity dominates
    assert event.source.chat_id.startswith("owner:")

    # But source metadata is preserved in raw_message
    raw = event.raw_message
    assert raw["raw_chat_id"] == "project:den-hermes-bridge:channel:42"
    assert raw["channel_id"] == 42
    assert raw["project_id"] == "den-hermes-bridge"

    # Delivery context records the session owner
    context = adapter._build_context(event)
    assert context is not None
    assert context.session_owner_id is not None
    assert context.session_owner_id.startswith("owner:")


@pytest.mark.asyncio
async def test_pool_worker_send_preserves_source_channel_routing() -> None:
    """Worker pool sends must route replies back to the source channel."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        700: {"id": 700, "channelId": 7, "senderIdentity": "runner", "body": "work"},
    }
    adapter = _pool_adapter(
        gateway, channels,
        pool_member_id="pool-coder-01",
        agent_instance_id="hermes:den-k8:spawned-coder:pool-coder-01:live",
    )

    event = await adapter.delivery_to_event(_delivery(1101, 700, attempt_id=1301, channel_id=7))
    adapter.set_delivery_context(event)

    result = await adapter.send(
        event.source.chat_id,
        "Worker reply.",
        metadata={"delivery_request_id": 1101, "notify": True},
    )

    assert result.success is True
    # Reply posted to the source channel (7), not to the session owner
    assert channels.posts[-1][0] == 7
    posted_payload = channels.posts[-1][1]
    assert posted_payload["sourceProjectId"] == "den-hermes-bridge"


# ---------------------------------------------------------------------------
# 6. Pure _resolve_session_owner unit tests
# ---------------------------------------------------------------------------

class TestResolveSessionOwner:
    """Unit tests for _resolve_session_owner precedence chain."""

    def test_explicit_session_owner_id_dominates(self) -> None:
        owner = _resolve_session_owner(
            {"project_id": "test"},
            {"session_owner_id": "agent-abc", "agentInstanceId": "other"},
            adapter_instance_id="adapter-xyz",
            pool_member_id="pool-1",
            agent_instance_id="inst-1",
            profile="prof",
        )
        assert owner == "owner:agent-abc"

    def test_session_owner_id_from_delivery(self) -> None:
        owner = _resolve_session_owner(
            {"sessionOwnerId": "def-456"},
            {},
            adapter_instance_id=None,
            pool_member_id=None,
            agent_instance_id=None,
            profile="prof",
        )
        assert owner == "owner:def-456"

    def test_delivery_agent_instance_id_second_priority(self) -> None:
        owner = _resolve_session_owner(
            {"project_id": "test"},
            {"agentInstanceId": "hermes:den-k8:runner:main"},
            adapter_instance_id="adapter-xyz",
            pool_member_id=None,
            agent_instance_id=None,
            profile="prof",
        )
        assert owner == "owner:hermes:den-k8:runner:main"

    def test_adapter_pool_member_fifth_priority(self) -> None:
        owner = _resolve_session_owner(
            {"project_id": "test"},
            {},
            adapter_instance_id="adapter-xyz",
            pool_member_id="pool-coder-01",
            agent_instance_id="inst-1",
            profile="spawned-coder",
        )
        assert owner == "owner:pool:pool-coder-01"

    def test_adapter_agent_instance_sixth_priority(self) -> None:
        owner = _resolve_session_owner(
            {"project_id": "test"},
            {},
            adapter_instance_id="adapter-xyz",
            pool_member_id=None,
            agent_instance_id="inst-42",
            profile="prof",
        )
        assert owner == "owner:inst-42"

    def test_assignment_id_third_priority(self) -> None:
        owner = _resolve_session_owner(
            {"project_id": "test"},
            {"assignment_id": 99},
            adapter_instance_id=None,
            pool_member_id=None,
            agent_instance_id=None,
            profile="prof",
        )
        assert owner == "owner:assignment:99"

    def test_assignment_id_overrides_adapter_pool_member(self) -> None:
        """Delivery-level assignment_id must produce distinct session even with adapter pool_member."""
        owner = _resolve_session_owner(
            {"project_id": "test"},
            {"assignment_id": 99},
            adapter_instance_id="adapter-xyz",
            pool_member_id="pool-coder-01",
            agent_instance_id="inst-1",
            profile="spawned-coder",
        )
        assert owner == "owner:assignment:99"

    def test_worker_run_id_fourth_priority(self) -> None:
        owner = _resolve_session_owner(
            {"project_id": "test"},
            {"workerRunId": "run-abc"},
            adapter_instance_id=None,
            pool_member_id=None,
            agent_instance_id=None,
            profile="prof",
        )
        assert owner == "owner:run:run-abc"

    def test_worker_run_id_overrides_adapter_pool_member(self) -> None:
        """Delivery-level worker_run_id must produce distinct session even with adapter pool_member."""
        owner = _resolve_session_owner(
            {"project_id": "test"},
            {"workerRunId": "run-abc"},
            adapter_instance_id="adapter-xyz",
            pool_member_id="pool-coder-01",
            agent_instance_id="inst-1",
            profile="spawned-coder",
        )
        assert owner == "owner:run:run-abc"

    def test_returns_none_when_no_owner_fields(self) -> None:
        owner = _resolve_session_owner(
            {"project_id": "test"},
            {"channel_id": 42},
            adapter_instance_id=None,
            pool_member_id=None,
            agent_instance_id=None,
            profile="some-profile",
        )
        assert owner is None

    def test_does_not_use_profile_to_collapse_workers(self) -> None:
        """Profile alone must NOT produce an owner key (prevents worker collapse)."""
        owner = _resolve_session_owner(
            {"project_id": "test"},
            {},
            adapter_instance_id=None,
            pool_member_id=None,
            agent_instance_id=None,
            profile="shared-profile",
        )
        # Must return None — not an owner keyed by profile name
        assert owner is None

    def test_empty_session_owner_id_falls_through(self) -> None:
        owner = _resolve_session_owner(
            {"project_id": "test"},
            {"session_owner_id": "  "},
            adapter_instance_id=None,
            pool_member_id=None,
            agent_instance_id=None,
            profile="prof",
        )
        assert owner is None


# ---------------------------------------------------------------------------
# 7. Session-owner / conversation-lane precedence integration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_session_owner_dominates_conversation_lane_for_durable_agent() -> None:
    """When both session owner and conversation lane are available, owner wins."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        800: {"id": 800, "channelId": 42, "senderIdentity": "ops", "body": "both present"},
    }
    adapter = _durable_adapter(gateway, channels)

    event = await adapter.delivery_to_event(
        _delivery(1201, 800, attempt_id=1401,
                  extra_metadata={"conversationLaneId": "some-lane"})
    )

    # Session owner should dominate, not the conversation lane
    assert event.source.chat_id.startswith("owner:"), (
        f"Session owner must dominate conversation lane: {event.source.chat_id!r}"
    )

    # conversation_lane_id in raw_message should still be present as source metadata
    # even though owner controls the chat_id
    assert event.raw_message["conversation_lane_id"] == "lane:some-lane", (
        f"Source lane metadata must be preserved: {event.raw_message['conversation_lane_id']!r}"
    )


@pytest.mark.asyncio
async def test_source_lane_scope_falls_back_to_conversation_lane() -> None:
    """Explicit source_lane scope uses conversation lane rather than owner."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        810: {"id": 810, "channelId": 42, "senderIdentity": "ops", "body": "no owner"},
    }
    # Adapter with no concrete identity
    adapter = DenChannelsAdapter(
        PlatformConfig(
            enabled=True,
            token="***",
            extra={
                "gateway_url": "http://192.168.1.10:18080",
                "channels_url": "http://192.168.1.10:18080",
                "project_id": "den-hermes-bridge",
                "agent_identity": "generic",
                "role": "agent",
                "profile": "generic",
                "start_claim_loop": False,
                "token": "test-token",
            },
        ),
        gateway_client=gateway,
        channels_client=channels,
    )

    event = await adapter.delivery_to_event(
        _delivery(1202, 810, attempt_id=1402,
                  extra_metadata={
                      "conversationLaneId": "fallback-lane",
                      "session_scope": "source_lane",
                  })
    )

    assert event.source.chat_id == "lane:fallback-lane"
    # Explicit source_lane scope opts out of session-owner resolution.
    assert event.raw_message["session_owner_id"] is None
    # conversation_lane metadata is preserved for routing
    assert event.raw_message.get("conversation_lane_id") == "lane:fallback-lane"


# ---------------------------------------------------------------------------
# 8. Delivery context has session_owner_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delivery_context_records_session_owner_id() -> None:
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        900: {"id": 900, "channelId": 42, "senderIdentity": "ops", "body": "context test"},
    }
    adapter = _durable_adapter(gateway, channels)

    event = await adapter.delivery_to_event(_delivery(1301, 900, attempt_id=1501))
    context = adapter._build_context(event)

    assert context is not None
    assert context.session_owner_id is not None
    assert context.session_owner_id.startswith("owner:")


# ---------------------------------------------------------------------------
# 9. Doc existence check
# ---------------------------------------------------------------------------

def test_session_owner_doc_exists() -> None:
    doc_path = Path(__file__).resolve().parents[1] / "docs" / "agent-instance-global-sessions-1890.md"
    assert doc_path.exists(), "Session owner ADR doc is missing"

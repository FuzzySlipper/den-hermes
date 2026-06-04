"""Tests for #1871: explicit Den Channels session lanes.

Session lane identity must be controlled by delivery metadata rather than
solely by the raw source channel id.  The adapter resolves a conversation
lane id from delivery metadata using a deterministic precedence chain,
and uses it as the Hermes session ``chat_id`` when present.

Lane-selection precedence:
  1. conversationLaneId / hermesSessionKey (explicit Den lane id)
  2. target_task_id (target-task-scoped lane)
  3. assignment_id / targetAssignmentId (assignment-scoped lane)
  4. worker_run_id / workerRunId (worker-run-scoped lane)
  5. None (fall back to source channel identity)

Acceptance criteria covered:
- Default project/channel lane continuity (same channel -> same session)
- Different channel/project separation
- Thread lane separation
- Same shared channel with different target tasks -> distinct sessions
- Same target lane across source channels reuses session with explicit lane id
- user_id present for auth/display does NOT fork the session key
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
_SPEC = importlib.util.spec_from_file_location("den_channels_adapter_1871", _ADAPTER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_adapter_module = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _adapter_module
_SPEC.loader.exec_module(_adapter_module)
DenChannelsAdapter = _adapter_module.DenChannelsAdapter
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

    async def get_gateway_message(self, message_id: str | int) -> dict[str, Any]:
        return dict(self.messages[int(message_id)])

    async def post_channel_message(self, channel_id: str | int, payload: dict[str, Any]) -> dict[str, Any]:
        self.posts.append((channel_id, payload))
        self._next_post_id += 1
        return {"id": self._next_post_id}


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
    metadata: dict[str, Any] = {"channel_id": channel_id, "channel_slug": "ops-hub", "session_scope": "source_lane"}
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
# 1. Default project/channel lane continuity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_default_channel_lane_continuity() -> None:
    """Same project/channel messages share context by default."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        100: {"id": 100, "channelId": 42, "senderIdentity": "alice", "body": "msg1"},
        101: {"id": 101, "channelId": 42, "senderIdentity": "bob", "body": "msg2"},
    }
    adapter = _adapter(gateway, channels)

    event_a = await adapter.delivery_to_event(_delivery(501, 100, attempt_id=701))
    event_b = await adapter.delivery_to_event(_delivery(502, 101, attempt_id=702))

    assert event_a.source.chat_id == event_b.source.chat_id
    assert event_a.source.chat_id == "project:den-hermes-bridge:channel:42"

    key_a = build_session_key(event_a.source, group_sessions_per_user=False)
    key_b = build_session_key(event_b.source, group_sessions_per_user=False)
    assert key_a == key_b


# ---------------------------------------------------------------------------
# 2. Different channel/project separation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_different_channels_produce_different_lanes() -> None:
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        200: {"id": 200, "channelId": 42, "senderIdentity": "user", "body": "ch42"},
        201: {"id": 201, "channelId": 99, "senderIdentity": "user", "body": "ch99"},
    }
    adapter = _adapter(gateway, channels)

    event_a = await adapter.delivery_to_event(_delivery(601, 200, attempt_id=801, channel_id=42))
    event_b = await adapter.delivery_to_event(_delivery(602, 201, attempt_id=802, channel_id=99))

    assert event_a.source.chat_id != event_b.source.chat_id
    key_a = build_session_key(event_a.source, group_sessions_per_user=False)
    key_b = build_session_key(event_b.source, group_sessions_per_user=False)
    assert key_a != key_b


# ---------------------------------------------------------------------------
# 3. Thread lane separation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_thread_lane_is_distinct_from_parent() -> None:
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        300: {"id": 300, "channelId": 42, "senderIdentity": "user", "body": "root"},
        301: {"id": 301, "channelId": 42, "senderIdentity": "user", "body": "thread", "threadRootMessageId": 5005},
    }
    adapter = _adapter(gateway, channels)

    root = await adapter.delivery_to_event(_delivery(701, 300, attempt_id=901))
    thread = await adapter.delivery_to_event(
        _delivery(702, 301, attempt_id=902, extra_metadata={"thread_root_message_id": 5005})
    )

    root_key = build_session_key(root.source, group_sessions_per_user=False)
    thread_key = build_session_key(thread.source, group_sessions_per_user=False)
    assert root_key != thread_key
    assert "5005" in thread_key


# ---------------------------------------------------------------------------
# 4. Same shared channel, different target tasks -> distinct session lanes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_same_channel_different_target_tasks_produce_distinct_sessions() -> None:
    """A shared worker-control channel can split sessions by target task id."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        400: {"id": 400, "channelId": 7, "senderIdentity": "runner", "body": "work on task 1871"},
        401: {"id": 401, "channelId": 7, "senderIdentity": "runner", "body": "work on task 1839"},
    }
    adapter = _adapter(gateway, channels)

    event_a = await adapter.delivery_to_event(
        _delivery(801, 400, attempt_id=1001, channel_id=7,
                  extra_metadata={"target_task_id": 1871})
    )
    event_b = await adapter.delivery_to_event(
        _delivery(802, 401, attempt_id=1002, channel_id=7,
                  extra_metadata={"target_task_id": 1839})
    )

    # Same source channel (7) but different target tasks -> different session lanes
    key_a = build_session_key(event_a.source, group_sessions_per_user=False)
    key_b = build_session_key(event_b.source, group_sessions_per_user=False)
    assert key_a != key_b, (
        f"Same channel with different target tasks must produce distinct sessions: "
        f"{key_a!r} == {key_b!r}"
    )
    # Both should reference their respective task ids
    assert "1871" in key_a
    assert "1839" in key_b


# ---------------------------------------------------------------------------
# 5. Same target lane across source channels reuses session
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_explicit_lane_id_reuses_session_across_source_channels() -> None:
    """Same explicit conversationLaneId from different source channels shares session."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        500: {"id": 500, "channelId": 10, "senderIdentity": "ops", "body": "from channel 10"},
        501: {"id": 501, "channelId": 20, "senderIdentity": "ops", "body": "from channel 20"},
    }
    adapter = _adapter(gateway, channels)

    lane_id = "shared-work-lane-abc"

    event_a = await adapter.delivery_to_event(
        _delivery(901, 500, attempt_id=1101, channel_id=10,
                  extra_metadata={"conversationLaneId": lane_id})
    )
    event_b = await adapter.delivery_to_event(
        _delivery(902, 501, attempt_id=1102, channel_id=20,
                  extra_metadata={"conversationLaneId": lane_id})
    )

    # Same explicit lane id -> same Hermes session key even from different channels
    key_a = build_session_key(event_a.source, group_sessions_per_user=False)
    key_b = build_session_key(event_b.source, group_sessions_per_user=False)
    assert key_a == key_b, (
        f"Same explicit lane id must share session across source channels: "
        f"{key_a!r} != {key_b!r}"
    )
    # The chat_id should reference the lane, not the raw channel
    assert event_a.source.chat_id == event_b.source.chat_id
    assert "lane:" in event_a.source.chat_id


# ---------------------------------------------------------------------------
# 6. user_id does not fork session key when explicit lane is present
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_user_id_does_not_fork_session_with_explicit_lane() -> None:
    """Sender identity must not affect lane session key."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        600: {"id": 600, "channelId": 42, "senderIdentity": "alice", "body": "msg from alice"},
        601: {"id": 601, "channelId": 42, "senderIdentity": "bob", "body": "msg from bob"},
    }
    adapter = _adapter(gateway, channels)

    # Both have the same explicit lane id
    lane_id = "project-x-control-lane"
    event_a = await adapter.delivery_to_event(
        _delivery(1001, 600, attempt_id=1201,
                  extra_metadata={"conversationLaneId": lane_id})
    )
    event_b = await adapter.delivery_to_event(
        _delivery(1002, 601, attempt_id=1202,
                  extra_metadata={"conversationLaneId": lane_id})
    )

    key_a = build_session_key(event_a.source, group_sessions_per_user=False)
    key_b = build_session_key(event_b.source, group_sessions_per_user=False)
    assert key_a == key_b

    # Verify user_id is not set on the source (adapter omits it by design)
    assert event_a.source.user_id is None
    assert event_b.source.user_id is None
    # But user_name is preserved for display
    assert event_a.source.user_name == "alice"
    assert event_b.source.user_name == "bob"


@pytest.mark.asyncio
async def test_user_id_does_not_fork_default_channel_session() -> None:
    """Even without explicit lane, different senders share the same channel session."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        700: {"id": 700, "channelId": 42, "senderIdentity": "alice", "body": "hi"},
        701: {"id": 701, "channelId": 42, "senderIdentity": "bob", "body": "hello"},
    }
    adapter = _adapter(gateway, channels)

    event_a = await adapter.delivery_to_event(_delivery(1101, 700, attempt_id=1301))
    event_b = await adapter.delivery_to_event(_delivery(1102, 701, attempt_id=1302))

    key_a = build_session_key(event_a.source, group_sessions_per_user=False)
    key_b = build_session_key(event_b.source, group_sessions_per_user=False)
    assert key_a == key_b
    assert "alice" not in key_a
    assert "bob" not in key_b


# ---------------------------------------------------------------------------
# 7. Lane-selection precedence tests
# ---------------------------------------------------------------------------

def test_precedence_explicit_lane_over_target_task() -> None:
    """conversationLaneId takes precedence over target_task_id."""
    lane = _resolve_conversation_lane(
        {"project_id": "test-proj", "target_task_id": 999},
        {"conversationLaneId": "explicit-lane", "target_task_id": 999},
    )
    assert lane == "lane:explicit-lane"


def test_precedence_target_task_over_assignment() -> None:
    """target_task_id takes precedence over assignment_id."""
    lane = _resolve_conversation_lane(
        {"project_id": "test-proj", "assignment_id": 50},
        {"target_task_id": 123, "assignment_id": 50},
    )
    assert lane == "lane:test-proj:task:123"


def test_precedence_assignment_over_worker_run() -> None:
    """assignment_id takes precedence over worker_run_id."""
    lane = _resolve_conversation_lane(
        {"project_id": "test-proj", "worker_run_id": "run-abc"},
        {"assignment_id": 77, "worker_run_id": "run-abc"},
    )
    assert lane == "lane:test-proj:assignment:77"


def test_precedence_worker_run_when_no_higher_fields() -> None:
    """worker_run_id is used when no higher-precedence fields are present."""
    lane = _resolve_conversation_lane(
        {"project_id": "test-proj"},
        {"worker_run_id": "dc-1871-run"},
    )
    assert lane == "lane:test-proj:run:dc-1871-run"


def test_precedence_none_when_no_lane_metadata() -> None:
    """Returns None when no lane metadata is present."""
    lane = _resolve_conversation_lane(
        {"project_id": "test-proj"},
        {"channel_id": 42},
    )
    assert lane is None


def test_hermes_session_key_alias() -> None:
    """hermesSessionKey is also accepted as an explicit lane id."""
    lane = _resolve_conversation_lane(
        {"project_id": "test-proj"},
        {"hermesSessionKey": "session-key-xyz"},
    )
    assert lane == "lane:session-key-xyz"


def test_camel_case_target_task_id() -> None:
    """targetTaskId (camelCase) is also accepted."""
    lane = _resolve_conversation_lane(
        {"project_id": "test-proj", "targetTaskId": 42},
        {},
    )
    assert lane == "lane:test-proj:task:42"


def test_camel_case_assignment() -> None:
    """targetAssignmentId is accepted at metadata level."""
    lane = _resolve_conversation_lane(
        {"project_id": "test-proj"},
        {"targetAssignmentId": 88},
    )
    assert lane == "lane:test-proj:assignment:88"


def test_camel_case_worker_run() -> None:
    """workerRunId is accepted at metadata level."""
    lane = _resolve_conversation_lane(
        {"project_id": "test-proj"},
        {"workerRunId": "run-456"},
    )
    assert lane == "lane:test-proj:run:run-456"


def test_empty_conversation_lane_id_falls_through() -> None:
    """Empty/whitespace conversationLaneId falls through to next level."""
    lane = _resolve_conversation_lane(
        {"project_id": "test-proj", "target_task_id": 42},
        {"conversationLaneId": "  "},
    )
    assert lane == "lane:test-proj:task:42"


# ---------------------------------------------------------------------------
# 8. Delivery context carries conversation_lane_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delivery_context_has_conversation_lane_id() -> None:
    """_DeliveryContext records the resolved lane id."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        800: {"id": 800, "channelId": 42, "senderIdentity": "user", "body": "lane test"},
    }
    adapter = _adapter(gateway, channels)

    event = await adapter.delivery_to_event(
        _delivery(1201, 800, attempt_id=1401,
                  extra_metadata={"conversationLaneId": "my-lane"})
    )
    context = adapter._build_context(event)
    assert context is not None
    assert context.conversation_lane_id == "lane:my-lane"


@pytest.mark.asyncio
async def test_delivery_context_has_no_conversation_lane_id_by_default() -> None:
    """_DeliveryContext has None conversation_lane_id when no lane metadata is present."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        801: {"id": 801, "channelId": 42, "senderIdentity": "user", "body": "plain"},
    }
    adapter = _adapter(gateway, channels)

    event = await adapter.delivery_to_event(_delivery(1202, 801, attempt_id=1402))
    context = adapter._build_context(event)
    assert context is not None
    assert context.conversation_lane_id is None


# ---------------------------------------------------------------------------
# 9. Raw channel metadata is preserved alongside lane
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_raw_chat_id_preserved_in_event_metadata() -> None:
    """Even with explicit lane, the raw channel chat_id is preserved in raw_message."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        900: {"id": 900, "channelId": 42, "senderIdentity": "user", "body": "test"},
    }
    adapter = _adapter(gateway, channels)

    event = await adapter.delivery_to_event(
        _delivery(1301, 900, attempt_id=1501,
                  extra_metadata={"conversationLaneId": "task-1871-lane"})
    )

    # The session-identity chat_id is the lane id
    assert "lane:" in event.source.chat_id
    # But the raw channel identity is preserved for routing back
    assert event.raw_message["raw_chat_id"] == "project:den-hermes-bridge:channel:42"
    assert event.raw_message["channel_id"] == 42


# ---------------------------------------------------------------------------
# 10. Lane context resolution for sends with explicit lanes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_resolves_context_by_explicit_lane_chat_id() -> None:
    """When an explicit lane is active, send() can find the context via lane chat_id."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        950: {"id": 950, "channelId": 42, "senderIdentity": "user", "body": "start"},
    }
    adapter = _adapter(gateway, channels)

    lane_id = "worker-control-lane"
    event = await adapter.delivery_to_event(
        _delivery(1401, 950, attempt_id=1601,
                  extra_metadata={"conversationLaneId": lane_id})
    )
    adapter.set_delivery_context(event)

    # Send using the lane chat_id (which is the event's chat_id)
    result = await adapter.send(
        event.source.chat_id,
        "Final reply via lane context.",
        metadata={"delivery_request_id": 1401, "notify": True},
    )

    assert result.success is True
    assert len(gateway.completed) == 1
    assert gateway.completed[0][0] == 1401


# ---------------------------------------------------------------------------
# 11. Assignment-scoped lanes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_assignment_scoped_lanes_are_distinct() -> None:
    """Same channel with different assignment ids produces different session lanes."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        960: {"id": 960, "channelId": 7, "senderIdentity": "runner", "body": "assignment 90"},
        961: {"id": 961, "channelId": 7, "senderIdentity": "runner", "body": "assignment 91"},
    }
    adapter = _adapter(gateway, channels)

    event_a = await adapter.delivery_to_event(
        _delivery(1501, 960, attempt_id=1701, channel_id=7,
                  extra_metadata={"target_assignment_id": 90})
    )
    event_b = await adapter.delivery_to_event(
        _delivery(1502, 961, attempt_id=1702, channel_id=7,
                  extra_metadata={"target_assignment_id": 91})
    )

    key_a = build_session_key(event_a.source, group_sessions_per_user=False)
    key_b = build_session_key(event_b.source, group_sessions_per_user=False)
    assert key_a != key_b
    assert "assignment:90" in key_a
    assert "assignment:91" in key_b


# ---------------------------------------------------------------------------
# 12. Worker-run-scoped lanes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worker_run_scoped_lanes_are_distinct() -> None:
    """Same channel with different worker run ids produces different session lanes."""
    gateway = FakeGatewayClient()
    channels = FakeChannelsClient()
    channels.messages = {
        970: {"id": 970, "channelId": 7, "senderIdentity": "runner", "body": "run a"},
        971: {"id": 971, "channelId": 7, "senderIdentity": "runner", "body": "run b"},
    }
    adapter = _adapter(gateway, channels)

    event_a = await adapter.delivery_to_event(
        _delivery(1601, 970, attempt_id=1801, channel_id=7,
                  extra_metadata={"worker_run_id": "dc-1871-coder-run"})
    )
    event_b = await adapter.delivery_to_event(
        _delivery(1602, 971, attempt_id=1802, channel_id=7,
                  extra_metadata={"worker_run_id": "dc-1871-reviewer-run"})
    )

    key_a = build_session_key(event_a.source, group_sessions_per_user=False)
    key_b = build_session_key(event_b.source, group_sessions_per_user=False)
    assert key_a != key_b
    assert "run:dc-1871-coder-run" in key_a
    assert "run:dc-1871-reviewer-run" in key_b


# ---------------------------------------------------------------------------
# 13. Doc existence check
# ---------------------------------------------------------------------------

def test_session_lanes_doc_exists() -> None:
    """The session lanes ADR should exist."""
    doc_path = Path(__file__).resolve().parents[1] / "docs" / "den-channels-session-lanes-1871.md"
    assert doc_path.exists(), "Session lanes ADR doc is missing"

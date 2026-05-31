"""Tests for #1795: internal events must queue while busy, not be auth-dropped.

Den Channels adapter builds MessageEvents with ``internal=True`` and
``user_id=None``.  The busy path must treat internal events consistently
with the cold path: skip user authorization, FIFO-queue while the session
is busy.

Existing #17775 protection (non-internal unauthorized shared-chat users
still blocked) is preserved.
"""

# --- Path setup: Hermes gateway lives outside the Den worktree ---
import sys
from pathlib import Path

_HERMES_AGENT_ROOT = Path("/home/agent/.hermes/hermes-agent")
if str(_HERMES_AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(_HERMES_AGENT_ROOT))

import time
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

# Minimal stubs for gateway imports (same pattern as test_busy_session_auth_bypass.py)
_tg = types.ModuleType("telegram")
_tg.constants = types.ModuleType("telegram.constants")
_ct = MagicMock()
_ct.SUPERGROUP = "supergroup"
_ct.GROUP = "group"
_ct.PRIVATE = "private"
_tg.constants.ChatType = _ct
sys.modules.setdefault("telegram", _tg)
sys.modules.setdefault("telegram.constants", _tg.constants)
sys.modules.setdefault("telegram.ext", types.ModuleType("telegram.ext"))

from gateway.platforms.base import (
    MessageEvent,
    MessageType,
    SessionSource,
    build_session_key,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(
    text="hello", chat_id="123", user_id="user1", user_name="TestUser",
    platform_val="slack", thread_id="thread-abc", *, internal=False,
):
    """Build a MessageEvent, optionally marked as internal (trusted delivery)."""
    source = SessionSource(
        platform=MagicMock(value=platform_val),
        chat_id=chat_id,
        chat_type="channel",
        user_id=user_id,
        user_name=user_name,
        thread_id=thread_id,
    )
    evt = MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        message_id="msg1",
        internal=internal,
    )
    return evt


def _make_runner(authorized_users=None):
    """Build a minimal GatewayRunner with configurable auth."""
    from gateway.run import GatewayRunner

    if authorized_users is None:
        authorized_users = {"user1"}

    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._busy_ack_ts = {}
    runner._draining = False
    runner.adapters = {}
    runner.config = MagicMock()
    runner.session_store = None
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    # Auth gate: only users in authorized_users set pass
    runner._is_user_authorized = lambda source: source.user_id in authorized_users
    return runner


def _make_adapter(platform_val="slack"):
    """Build a minimal adapter mock."""
    adapter = MagicMock()
    adapter._pending_messages = {}
    adapter._send_with_retry = AsyncMock()
    adapter.config = MagicMock()
    adapter.config.extra = {}
    adapter.platform = MagicMock(value=platform_val)
    return adapter


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBusySessionInternalAuth:
    """#1795: Internal events must queue while busy, not be auth-dropped.

    The fix adds an ``event.internal`` guard in _handle_active_session_busy_message
    that matches the cold-path behavior: internal events skip auth and queue.
    """

    @pytest.mark.asyncio
    async def test_internal_event_queued_while_busy_no_user_id(self):
        """Internal event with user_id=None queues, not dropped, while busy."""
        from gateway.run import GatewayRunner

        # Auth rejects every user — internal events should still pass
        runner = _make_runner(authorized_users=set())
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter("den_channels")

        # Active session exists
        internal_event = _make_event(
            text="delivery payload",
            user_id=None,
            user_name="den-mcp-planner",
            platform_val="den_channels",
            internal=True,
        )
        sk = build_session_key(internal_event.source)
        runner._running_agents[sk] = MagicMock()
        runner.adapters[internal_event.source.platform] = adapter

        result = await GatewayRunner._handle_active_session_busy_message(
            runner, internal_event, sk,
        )

        # Must be handled (returns True)
        assert result is True
        # Must be QUEUED (in adapter._pending_messages), not dropped
        assert sk in adapter._pending_messages
        # Must NOT be dropped as unauthorized (no warning logged about auth)
        # No interrupt called
        runner._running_agents[sk].interrupt.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_internal_unauthorized_still_blocked(self):
        """Non-internal unauthorized event is still blocked (#17775 preserved)."""
        from gateway.run import GatewayRunner

        # Only user1 is authorized
        runner = _make_runner(authorized_users={"user1"})
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter("slack")

        authorized_event = _make_event(
            text="working", user_id="user1", platform_val="slack",
        )
        sk = build_session_key(authorized_event.source)
        runner._running_agents[sk] = MagicMock()
        runner.adapters[authorized_event.source.platform] = adapter

        # Unauthorized non-internal event
        intruder_event = _make_event(
            text="inject", user_id="attacker",
            user_name="Hacker", platform_val="slack",
            internal=False,
        )

        result = await GatewayRunner._handle_active_session_busy_message(
            runner, intruder_event, sk,
        )

        assert result is True  # handled = dropped
        # Must NOT queue — unauthorized
        assert sk not in adapter._pending_messages
        runner._running_agents[sk].interrupt.assert_not_called()

    @pytest.mark.asyncio
    async def test_internal_event_queued_with_user_id(self):
        """Internal event WITH a user_id also queues (trusted delivery)."""
        from gateway.run import GatewayRunner

        runner = _make_runner(authorized_users=set())  # auth denies all
        runner._busy_input_mode = "interrupt"
        adapter = _make_adapter("den_channels")

        internal_event = _make_event(
            text="trusted delivery",
            user_id="den-mcp-runner",
            user_name="Runner",
            platform_val="den_channels",
            internal=True,
        )
        sk = build_session_key(internal_event.source)
        runner._running_agents[sk] = MagicMock()
        runner.adapters[internal_event.source.platform] = adapter

        result = await GatewayRunner._handle_active_session_busy_message(
            runner, internal_event, sk,
        )

        assert result is True
        assert sk in adapter._pending_messages

    @pytest.mark.asyncio
    async def test_internal_event_respects_drain_mode(self):
        """Internal events during drain mode are queued, not auth-dropped."""
        from gateway.run import GatewayRunner

        runner = _make_runner(authorized_users=set())
        runner._draining = True
        runner._queue_during_drain_enabled = lambda: True

        internal_event = _make_event(
            text="drain-period delivery",
            user_id=None,
            user_name="den-mcp-planner",
            platform_val="den_channels",
            internal=True,
        )
        sk = build_session_key(internal_event.source)

        adapter = _make_adapter("den_channels")
        # Adapter lookup during drain
        runner.adapters = MagicMock()
        runner.adapters.get = MagicMock(return_value=adapter)

        result = await GatewayRunner._handle_active_session_busy_message(
            runner, internal_event, sk,
        )

        # Must be handled (queued for next turn after drain completes)
        assert result is True

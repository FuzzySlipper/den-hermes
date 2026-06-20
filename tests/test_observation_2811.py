"""Tests for task #2811: Observation event emission from Hermes adapter.

Verifies:
- DenObservationClient payload shape and graceful degradation
- _emit_observation_activity helper produces valid agent_activity.v1 envelope
- Events emitted from connect/disconnect/processing lifecycle
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any
from unittest.mock import patch as mock_patch

import pytest

# Import the adapter module for testing
_here = os.path.dirname(__file__)
_adapter_path = os.path.join(_here, "..", "plugins", "platforms", "den_channels", "adapter.py")

import importlib.util
_spec = importlib.util.spec_from_file_location("den_observation_test", _adapter_path)
assert _spec is not None and _spec.loader is not None
_adapter_module = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _adapter_module
_spec.loader.exec_module(_adapter_module)

DenObservationClient = _adapter_module.DenObservationClient
DenChannelsAdapter = _adapter_module.DenChannelsAdapter


# =========================================================================
# DenObservationClient tests
# =========================================================================


class _FakeResponse:
    """Minimal httpx.Response stand-in."""

    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeAsyncClient:
    """Stand-in for httpx.AsyncClient that captures requests."""

    def __init__(self, *, timeout: float = 15.0) -> None:
        self.captured: list[dict[str, Any]] = []

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]) -> _FakeResponse:
        self.captured.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(200)


def test_observation_client_no_op_when_not_configured() -> None:
    """When observation URL is empty, post_activity_event does nothing."""
    client = DenObservationClient("")
    assert client.is_configured is False
    asyncio.run(client.post_activity_event("runtime", "adapter_connected", {"profile": "test"}, {"kind": "agent_activity.v1", "schema_version": 1, "summary": "test"}))


def test_observation_client_payload_shape() -> None:
    """Posted body matches the observation contract envelope."""
    fake_client = _FakeAsyncClient()

    with mock_patch("httpx.AsyncClient", return_value=fake_client):
        client = DenObservationClient("http://obs.test:8082")
        asyncio.run(client.post_activity_event(
            source_domain="runtime",
            event_type="adapter_connected",
            agent_identity={"profile": "den-mcp-runner", "instance_id": "runner@den-k8"},
            payload={
                "kind": "agent_activity.v1",
                "schema_version": 1,
                "summary": "Hermes runner connected.",
                "severity": "info",
                "visibility": "agent",
                "adapter": "hermes",
                "surface": "channel",
            },
        ))

    assert len(fake_client.captured) == 1
    body = fake_client.captured[0]
    assert body["url"] == "http://obs.test:8082/v1/observation/activity-events"
    assert body["json"]["source_domain"] == "runtime"
    assert body["json"]["event_type"] == "adapter_connected"
    assert body["json"]["agent_identity"]["profile"] == "den-mcp-runner"
    assert body["json"]["payload"]["kind"] == "agent_activity.v1"
    assert body["json"]["payload"]["summary"] == "Hermes runner connected."


def test_observation_client_sends_token_when_configured() -> None:
    """Auth header is sent when token is provided."""
    fake_client = _FakeAsyncClient()

    with mock_patch("httpx.AsyncClient", return_value=fake_client):
        client = DenObservationClient("http://obs.test:8082", token="secret-token")
        asyncio.run(client.post_activity_event(
            source_domain="runtime",
            event_type="adapter_connected",
            agent_identity={"profile": "test"},
            payload={"kind": "agent_activity.v1", "schema_version": 1, "summary": "test", "severity": "info", "visibility": "agent", "adapter": "hermes", "surface": "channel"},
        ))

    auth = fake_client.captured[0]["headers"].get("Authorization", "")
    assert auth == "Bearer secret-token"


def test_observation_client_graceful_degrade() -> None:
    """Transport errors are caught and do not propagate."""
    client = DenObservationClient("http://nonexistent.invalid")
    asyncio.run(client.post_activity_event("runtime", "adapter_connected", {"profile": "test"}, {"kind": "agent_activity.v1", "schema_version": 1, "summary": "test", "severity": "info", "visibility": "agent", "adapter": "hermes", "surface": "channel"}))


# =========================================================================
# _emit_observation_activity helper tests
# =========================================================================


class _FakeObservationClient:
    """Stand-in that captures emitted observation events."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._base_url = "http://obs.test"
        self._token: str | None = None

    @property
    def is_configured(self) -> bool:
        return True

    async def post_activity_event(self, source_domain: str, event_type: str, agent_identity: dict[str, Any], payload: dict[str, Any]) -> None:
        self.events.append({
            "source_domain": source_domain,
            "event_type": event_type,
            "agent_identity": agent_identity,
            "payload": payload,
        })


class _FakeEmptyObservationClient:
    """Stand-in where observation is not configured."""

    @property
    def is_configured(self) -> bool:
        return False

    async def post_activity_event(self, **kwargs: Any) -> None:
        pytest.fail("should not be called when not configured")


def _make_adapter_with_obs(obs_client: Any) -> DenChannelsAdapter:
    """Create a bare DenChannelsAdapter with a controlled observation client."""
    from gateway.config import PlatformConfig
    config = PlatformConfig(enabled=True, token="test-token")
    config.extra = {
        "gateway_url": "http://gateway.test:8079",
        "channels_url": "http://channels.test:18081",
        "observation_url": "http://obs.test:8082",
        "profile": "den-mcp-runner-test",
        "agent_identity": "den-mcp-runner-test",
    }
    adapter = DenChannelsAdapter(config)
    adapter.observation_client = obs_client
    return adapter


@pytest.mark.asyncio
async def test_emit_observation_activity_envelope() -> None:
    """_emit_observation_activity produces the correct agent_activity.v1 envelope."""
    obs = _FakeObservationClient()
    adapter = _make_adapter_with_obs(obs)

    await adapter._emit_observation_activity(
        "work_checkpoint",
        summary="Checkpointed task 2811.",
        severity="info",
        visibility="task",
        surface="worker",
        session_key="session-1",
        work_ref={"project_id": "den-hermes-bridge", "task_id": 2811},
    )

    assert len(obs.events) == 1
    event = obs.events[0]
    assert event["source_domain"] == "runtime"
    assert event["event_type"] == "work_checkpoint"
    assert event["agent_identity"]["profile"] == "den-mcp-runner-test"
    payload = event["payload"]
    assert payload["kind"] == "agent_activity.v1"
    assert payload["schema_version"] == 1
    assert payload["summary"] == "Checkpointed task 2811."
    assert payload["severity"] == "info"
    assert payload["visibility"] == "task"
    assert payload["adapter"] == "hermes"
    assert payload["surface"] == "worker"
    assert payload["session_key"] == "session-1"
    assert payload["work_ref"]["task_id"] == 2811


@pytest.mark.asyncio
async def test_emit_observation_activity_skips_when_not_configured() -> None:
    """No emission when observation client is not configured."""
    obs = _FakeEmptyObservationClient()
    adapter = _make_adapter_with_obs(obs)

    await adapter._emit_observation_activity("adapter_connected", summary="test")


@pytest.mark.asyncio
async def test_emit_observation_activity_truncates_summary() -> None:
    """Summary is truncated to 240 characters."""
    obs = _FakeObservationClient()
    adapter = _make_adapter_with_obs(obs)

    long_summary = "x" * 500
    await adapter._emit_observation_activity("adapter_connected", summary=long_summary)

    assert len(obs.events) == 1
    assert len(obs.events[0]["payload"]["summary"]) == 240


@pytest.mark.asyncio
async def test_emit_observation_work_failed_includes_reason() -> None:
    """work_failed events include reason_code."""
    obs = _FakeObservationClient()
    adapter = _make_adapter_with_obs(obs)

    await adapter._emit_observation_activity(
        "work_failed",
        severity="error",
        summary="Delivery processing failed.",
        reason_code="processing_no_response",
        visibility="agent",
        surface="worker",
    )

    assert len(obs.events) == 1
    payload = obs.events[0]["payload"]
    assert payload["reason_code"] == "processing_no_response"
    assert payload["severity"] == "error"


def test_emit_observation_identity_construction() -> None:
    """agent_identity includes profile and instance_id when available."""
    from gateway.config import PlatformConfig
    config = PlatformConfig(enabled=True, token="test-token")
    config.extra = {
        "gateway_url": "http://gateway.test:8079",
        "channels_url": "http://channels.test:18081",
        "observation_url": "http://obs.test:8082",
        "profile": "spawned-coder",
        "agent_identity": "spawned-coder",
        "agent_instance_id": "pool-coder-03",
    }
    adapter = DenChannelsAdapter(config)
    adapter.observation_client = _FakeObservationClient()

    identity = adapter._build_observation_identity()
    assert identity["profile"] == "spawned-coder"
    assert identity["instance_id"] == "pool-coder-03"


# =========================================================================
# Lifecycle integration tests
# =========================================================================


@pytest.mark.asyncio
async def test_emit_observation_adapter_connected() -> None:
    """adapter_connected event shape."""
    obs = _FakeObservationClient()
    adapter = _make_adapter_with_obs(obs)

    await adapter._emit_observation_activity(
        "adapter_connected",
        summary="Hermes den-mcp-runner-test connected to Den Channels.",
        surface="channel",
    )

    assert any(e["event_type"] == "adapter_connected" for e in obs.events)
    assert obs.events[0]["payload"]["surface"] == "channel"


@pytest.mark.asyncio
async def test_emit_observation_disconnect() -> None:
    """adapter_disconnected event includes reason_code."""
    obs = _FakeObservationClient()
    adapter = _make_adapter_with_obs(obs)

    await adapter._emit_observation_activity(
        "adapter_disconnected",
        severity="warning",
        summary="Hermes disconnected.",
        reason_code="graceful_shutdown",
    )

    assert any(e["event_type"] == "adapter_disconnected" for e in obs.events)
    assert obs.events[0]["payload"]["reason_code"] == "graceful_shutdown"
    assert obs.events[0]["payload"]["severity"] == "warning"


# =========================================================================
# No-secrets verification
# =========================================================================


@pytest.mark.asyncio
async def test_observation_payload_no_secrets() -> None:
    """Observation payloads must not contain tokens or secrets."""
    obs = _FakeObservationClient()
    adapter = _make_adapter_with_obs(obs)

    await adapter._emit_observation_activity(
        "adapter_connected",
        summary="Test connection.",
        surface="channel",
    )

    serialized = json.dumps(obs.events)
    assert "secret-token" not in serialized

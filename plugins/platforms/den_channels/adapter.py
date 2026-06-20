"""Native Hermes adapter for Den Channels durable sessions.

This adapter treats Den Channels as a first-class Hermes platform.  It
claims delivery requests from Den Channels, turns them into normal runner
``MessageEvent`` objects with stable Den Channels session lanes, and posts
final assistant replies back to Den Channels as ``human_text`` or
``gateway_delivery`` messages.

Breadcrumb / tool-activity emission posts to ``POST /api/channel-activity-events``
on the configured Channels base URL (``channels_url`` / ``channelsUrl``),
with ``gateway_url`` / ``gatewayUrl`` as a compatibility fallback.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import inspect
import json
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit

try:
    from den_hermes.api_urls import join_api_url
except ModuleNotFoundError:  # pragma: no cover - exercised by plugin-only installs
    def join_api_url(base_url: str, path: str) -> str:
        """Fallback for clean Hermes plugin roots without the den_hermes package."""
        normalized = base_url.rstrip("/")
        for suffix in ("/api/gateway", "/api"):
            if normalized.endswith(suffix):
                normalized = normalized[: -len(suffix)]
                break
        return f"{normalized}{path}"

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
)
from gateway.session import build_session_key

logger = logging.getLogger(__name__)

_PLATFORM_NAME = "den_channels"
_SECRET_KEY_FRAGMENTS = ("api_key", "apikey", "token", "secret", "password", "credential")
_ACTIVITY_CONTEXT_ENV = "DEN_CHANNELS_ACTIVITY_CONTEXT"
_ACTIVITY_CONTEXT_VAR: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "den_channels_activity_context",
    default={},
)
_ACTIVITY_MAX_TEXT = 700
_ACTIVITY_MAX_JSON = 1400
_ACTIVITY_STATES: dict[str, dict[str, Any]] = {}
_ACTIVITY_LOCK = threading.Lock()


def _extra(config: PlatformConfig) -> dict[str, Any]:
    value = getattr(config, "extra", None)
    return value if isinstance(value, dict) else {}


def _first(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _json_obj(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _coerce_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_int_list(value: Any) -> list[int]:
    """Parse a config value into a de-duplicated list of positive integers."""
    if value is None or value == "":
        return []
    raw_items: list[Any]
    if isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    elif isinstance(value, str):
        raw_items = [part.strip() for part in value.replace(";", ",").split(",")]
    else:
        raw_items = [value]

    seen: set[int] = set()
    result: list[int] = []
    for item in raw_items:
        parsed = _coerce_int(item)
        if parsed is None or parsed <= 0 or parsed in seen:
            continue
        seen.add(parsed)
        result.append(parsed)
    return result




def _coerce_int_mapping(value: Any) -> dict[int, int]:
    """Parse a config mapping of channel id -> initial after id cursors."""
    if value is None or value == "":
        return {}
    raw: dict[Any, Any] = {}
    if isinstance(value, dict):
        raw = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                raw = parsed
            else:
                return {}
        except Exception:
            for part in text.replace(";", ",").split(","):
                if ":" not in part:
                    continue
                key, item = part.split(":", 1)
                raw[key.strip()] = item.strip()
    else:
        return {}

    result: dict[int, int] = {}
    for key, item in raw.items():
        channel_id = _coerce_int(key)
        after_id = _coerce_int(item)
        if channel_id is None or channel_id <= 0 or after_id is None or after_id < 0:
            continue
        result[channel_id] = after_id
    return result

def _redact(value: Any) -> Any:
    """Recursively redact secret-looking keys and placeholder secret values."""
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if any(fragment in key_text for fragment in _SECRET_KEY_FRAGMENTS):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        text = value.strip()
        lowered = text.lower()
        if text == "***" or any(marker in lowered for marker in ("bearer ", "api_key", "token=")):
            return "[REDACTED]"
    return value


def _truncate_text(value: Any, limit: int = _ACTIVITY_MAX_TEXT) -> str:
    text = str(value if value is not None else "")
    if len(text) <= limit:
        return text
    return f"{text[:limit - 18]}… [truncated]"


def _safe_json_preview(value: Any, limit: int = _ACTIVITY_MAX_JSON) -> str:
    redacted = _redact(value)
    try:
        text = json.dumps(redacted, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        text = json.dumps(str(redacted), ensure_ascii=False)
    return _truncate_text(text, limit)


def _tool_name_from_kwargs(kwargs: dict[str, Any]) -> str:
    for key in ("tool_name", "name", "function_name"):
        value = kwargs.get(key)
        if value:
            return str(value)
    tool_call = kwargs.get("tool_call")
    if isinstance(tool_call, dict):
        fn = tool_call.get("function")
        if isinstance(fn, dict) and fn.get("name"):
            return str(fn["name"])
        if tool_call.get("name"):
            return str(tool_call["name"])
    return "tool"


def _tool_args_from_kwargs(kwargs: dict[str, Any]) -> Any:
    for key in ("args", "arguments", "tool_args"):
        if key in kwargs:
            return kwargs[key]
    tool_call = kwargs.get("tool_call")
    if isinstance(tool_call, dict):
        fn = tool_call.get("function")
        if isinstance(fn, dict) and "arguments" in fn:
            raw = fn["arguments"]
            if isinstance(raw, str):
                try:
                    return json.loads(raw)
                except Exception:
                    return raw
            return raw
    return {}


def normalize_tool_activity(tool_name: str, args: Any, *, status: str = "started", count: int = 1, duration_ms: int | None = None) -> dict[str, Any]:
    """Normalize a Hermes tool call into a bounded Den activity payload."""
    safe_name = _truncate_text(tool_name or "tool", 120)
    preview = _safe_json_preview(args)
    suffix = f" ×{count}" if count > 1 else ""
    summary = f"{safe_name}{suffix}: {preview}"
    metadata: dict[str, Any] = {"tool_name": safe_name, "count": count}
    if duration_ms is not None:
        metadata["duration_ms"] = duration_ms
    event_type = {
        "started": "tool_call_started",
        "completed": "tool_call_completed",
        "failed": "tool_call_failed",
    }.get(status, "tool_call_started")
    return {
        "eventType": event_type,
        "status": status,
        "title": safe_name,
        "summary": _truncate_text(summary, 1000),
        "previewJson": preview,
        "metadataJson": _safe_json_preview(metadata),
    }


def _activity_context() -> dict[str, Any]:
    context = _ACTIVITY_CONTEXT_VAR.get({})
    if context:
        return dict(context)
    return _json_obj(os.getenv(_ACTIVITY_CONTEXT_ENV, ""))


def _activity_state_key(context: dict[str, Any]) -> str:
    display_block_id = context.get("displayBlockId") or context.get("display_block_id")
    worker_run_id = context.get("workerRunId") or context.get("worker_run_id")
    worker_role = context.get("workerRole") or context.get("worker_role")
    if display_block_id and worker_run_id and worker_role:
        return f"display:{display_block_id}:worker:{worker_run_id}:role:{worker_role}"
    return str(context.get("deliveryRequestId") or context.get("delivery_request_id") or context.get("sessionKey") or "")


def _activity_dedupe_key(context: dict[str, Any], *, state_key: str, sequence: int, tool_name: str, preview: str) -> str:
    display_block_id = context.get("displayBlockId") or context.get("display_block_id")
    worker_run_id = context.get("workerRunId") or context.get("worker_run_id")
    worker_role = context.get("workerRole") or context.get("worker_role")
    if display_block_id and worker_run_id and worker_role:
        return f"activity:{display_block_id}:{worker_run_id}:{worker_role}:tool:{sequence}"
    digest = hashlib.sha1(f"{state_key}:{sequence}:{tool_name}:{preview}".encode()).hexdigest()[:12]
    return f"activity:{state_key}:tool:{sequence}:{digest}"


def _json_dict_from_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            loaded = json.loads(value)
        except Exception:
            return {}
        if isinstance(loaded, dict):
            return loaded
    return {}


def _emit_activity_event(context: dict[str, Any], payload: dict[str, Any]) -> None:
    """Post a tool-activity event to Den Channels.

    Prefers ``observationUrl`` / ``observation_url`` / ``DEN_OBSERVATION_URL``
    as the base URL for activity/observation events. Falls back to
    ``channelsUrl`` / ``channels_url`` / ``DEN_CHANNELS_URL`` for backward
    compatibility with profiles that have not yet migrated to an explicit
    observation URL.
    The ``gatewayUrl`` / ``gateway_url`` fallback is removed as of #2786.
    """
    base_url = str(
        context.get("observationUrl") or context.get("observation_url")
        or context.get("channelsUrl") or context.get("channels_url")
        or ""
    ).rstrip("/")
    channel_id = context.get("channelId") or context.get("channel_id")
    if not base_url or not channel_id:
        return
    request_payload = {
        "channelId": str(channel_id),
        "projectId": context.get("projectId") or context.get("project_id"),
        "agentIdentity": context.get("agentIdentity") or context.get("agent_identity") or "hermes",
        "deliveryRequestId": str(context.get("deliveryRequestId") or context.get("delivery_request_id") or "") or None,
        "hermesSessionKey": context.get("hermesSessionKey") or context.get("sessionKey") or context.get("session_key"),
        "taskId": _coerce_int(context.get("taskId") or context.get("task_id")),
        "threadId": _coerce_int(context.get("threadId") or context.get("thread_id")),
        "anchorMessageId": _coerce_int(context.get("anchorMessageId") or context.get("anchor_message_id")),
        **payload,
    }
    forwarded = {
        "displayBlockId": context.get("displayBlockId") or context.get("display_block_id"),
        "parentHermesSessionKey": context.get("parentHermesSessionKey") or context.get("parent_hermes_session_key"),
        "parentAgentIdentity": context.get("parentAgentIdentity") or context.get("parent_agent_identity"),
        "workerRunId": context.get("workerRunId") or context.get("worker_run_id"),
        "workerRole": context.get("workerRole") or context.get("worker_role"),
    }
    request_payload.update({key: value for key, value in forwarded.items() if value not in {None, ""}})
    metadata = _json_dict_from_payload(request_payload.get("metadataJson"))
    if forwarded["workerRunId"] not in {None, ""}:
        metadata["workerRunId"] = forwarded["workerRunId"]
    if forwarded["workerRole"] not in {None, ""}:
        metadata["workerRole"] = forwarded["workerRole"]
    if metadata:
        request_payload["metadataJson"] = _safe_json_preview(metadata)
    headers = {"Content-Type": "application/json"}
    token = str(context.get("token") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        import httpx

        with httpx.Client(timeout=2.0) as client:
            response = client.post(f"{base_url}/api/channel-activity-events", json=request_payload, headers=headers)
            response.raise_for_status()
    except Exception:
        logger.debug("[DenChannels] activity event emission failed", exc_info=True)


def _on_pre_tool_call(**kwargs: Any) -> None:
    context = _activity_context()
    key = _activity_state_key(context)
    if not key:
        return
    tool_name = _tool_name_from_kwargs(kwargs)
    args = _tool_args_from_kwargs(kwargs)
    preview = _safe_json_preview(args)
    signature = (tool_name, preview)
    tool_call_id = str(kwargs.get("tool_call_id") or kwargs.get("call_id") or "")
    with _ACTIVITY_LOCK:
        state = _ACTIVITY_STATES.setdefault(key, {"sequence": 0, "last": None, "calls": {}})
        if state.get("last") and state["last"].get("signature") == signature:
            item = state["last"]
            item["count"] += 1
        else:
            state["sequence"] += 1
            sequence = state["sequence"]
            item = {
                "signature": signature,
                "count": 1,
                "sequence": sequence,
                "dedupeKey": _activity_dedupe_key(
                    context,
                    state_key=key,
                    sequence=sequence,
                    tool_name=tool_name,
                    preview=preview,
                ),
            }
            state["last"] = item
        if tool_call_id:
            state["calls"][tool_call_id] = item
        payload = normalize_tool_activity(tool_name, args, status="started", count=item["count"])
        payload.update({"sequence": item["sequence"], "dedupeKey": item["dedupeKey"]})
    try:
        _emit_activity_event(context, payload)
    except Exception:
        logger.debug("[DenChannels] pre-tool activity hook failed", exc_info=True)


def _on_post_tool_call(**kwargs: Any) -> None:
    context = _activity_context()
    key = _activity_state_key(context)
    if not key:
        return
    tool_name = _tool_name_from_kwargs(kwargs)
    args = _tool_args_from_kwargs(kwargs)
    tool_call_id = str(kwargs.get("tool_call_id") or kwargs.get("call_id") or "")
    error = kwargs.get("error") or kwargs.get("exception")
    status = "failed" if error else "completed"
    duration_ms = _coerce_int(kwargs.get("duration_ms") or kwargs.get("elapsed_ms"))
    with _ACTIVITY_LOCK:
        state = _ACTIVITY_STATES.get(key) or {}
        item = (state.get("calls") or {}).get(tool_call_id) if tool_call_id else None
        item = item or state.get("last")
        if not item:
            return
        payload = normalize_tool_activity(tool_name, args, status=status, count=item.get("count", 1), duration_ms=duration_ms)
        payload.update({"sequence": item["sequence"], "dedupeKey": item["dedupeKey"]})
    try:
        _emit_activity_event(context, payload)
    except Exception:
        logger.debug("[DenChannels] post-tool activity hook failed", exc_info=True)


def _is_private_url(url: str) -> bool:
    """Return True for loopback/private Den service URLs trusted as internal."""
    host = (urlsplit(url or "").hostname or "").strip().lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        import ipaddress

        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        return False


@dataclass
class _DeliveryContext:
    delivery_request_id: int
    attempt_id: Optional[int]
    project_id: str
    channel_id: int
    trigger_message_id: Optional[int]
    thread_root_message_id: Optional[int]
    session_key: str
    session_id: Optional[str]
    original_source_kind: str
    original_source_id: str
    raw_delivery: dict[str, Any]
    conversation_lane_id: Optional[str] = None
    """Explicit Den conversation/source-lane id carried in delivery metadata.

    This is compatibility/source-lane identity. When ``session_owner_id`` is
    present, the session owner dominates and this field records only the source
    lane for routing/display.
    """
    session_owner_id: Optional[str] = None
    """Resolved session-owner identity from #1890.

    When present, this is the concrete agent/worker identity that owns the
    Hermes session. It overrides channel-lane session identity for durable
    agents and distinct worker instances.
    """


_DIRECT_AGENT_CONFIG_DEFAULTS: dict[str, str] = {}


def _resolve_session_owner(
    delivery: dict[str, Any],
    metadata: dict[str, Any],
    adapter_instance_id: Optional[str],
    pool_member_id: Optional[str],
    agent_instance_id: Optional[str],
    profile: str,
) -> Optional[str]:
    """Resolve the Hermes session owner for a delivery.

    Session-owner precedence (#1890): explicit session owner, concrete
    delivery agent instance, assignment, worker run, then adapter-level
    concrete identity.  A delivery can opt back into #1871 source-lane
    compatibility by setting ``session_scope``/``sessionScope`` to
    ``source_lane``.
    """
    scope = (
        _first(metadata, "session_scope", "sessionScope")
        or _first(delivery, "session_scope", "sessionScope")
        or ""
    )
    if str(scope).strip().lower() in {"source_lane", "source-lane", "channel", "source_channel"}:
        return None

    explicit_owner = _first(metadata, "session_owner_id", "sessionOwnerId") or _first(
        delivery, "session_owner_id", "sessionOwnerId"
    )
    if explicit_owner is not None and str(explicit_owner).strip():
        return f"owner:{str(explicit_owner).strip()}"

    delivery_agent_instance = _first(metadata, "agent_instance_id", "agentInstanceId") or _first(
        delivery, "agent_instance_id", "agentInstanceId"
    )
    if delivery_agent_instance is not None and str(delivery_agent_instance).strip():
        return f"owner:{str(delivery_agent_instance).strip()}"

    assignment_id = _first(
        metadata, "assignment_id", "targetAssignmentId", "target_assignment_id", "assignmentId"
    ) or _first(
        delivery, "assignment_id", "targetAssignmentId", "target_assignment_id", "assignmentId"
    )
    if assignment_id is not None:
        return f"owner:assignment:{assignment_id}"

    worker_run_id = _first(metadata, "worker_run_id", "workerRunId") or _first(
        delivery, "worker_run_id", "workerRunId"
    )
    if worker_run_id is not None:
        return f"owner:run:{worker_run_id}"

    if pool_member_id:
        return f"owner:pool:{pool_member_id}"
    if agent_instance_id:
        return f"owner:{agent_instance_id}"
    if adapter_instance_id:
        return f"owner:{adapter_instance_id}"

    # Never collapse distinct workers solely by shared profile identity.
    if profile:
        return None
    return None


def _resolve_conversation_lane(
    delivery: dict[str, Any],
    metadata: dict[str, Any],
) -> Optional[str]:
    """Resolve an explicit conversation lane id from delivery metadata.

    Returns a lane id string to use as the Hermes session ``chat_id``, or
    ``None`` to fall back to the default ``project:<id>:channel:<id>`` key.

    Lane-selection precedence (#1871):

      1. ``conversationLaneId`` / ``hermesSessionKey`` — explicit Den-owned
         lane id provided by Core/Channels/Gateway.
      2. ``target_task_id`` / ``targetTaskId`` — target-task-scoped lane.
      3. ``assignment_id`` / ``targetAssignmentId`` — assignment-scoped lane.
      4. ``worker_run_id`` / ``workerRunId`` — worker-run-scoped lane.
      5. ``None`` — fall back to source channel identity.

    Levels 2-4 construct a synthetic lane id of the form
    ``lane:<project>:task:<id>``, ``lane:<project>:assignment:<id>``, or
    ``lane:<project>:run:<id>`` respectively, where ``<project>`` comes from
    the delivery's ``project_id``.
    """
    # Level 1: Explicit lane id from Den Core/Channels/Gateway.
    explicit = _first(
        metadata,
        "conversationLaneId",
        "conversation_lane_id",
        "hermesSessionKey",
        "hermes_session_key",
    )
    if explicit is not None and str(explicit).strip():
        return f"lane:{str(explicit).strip()}"

    project_id = str(
        _first(delivery, "project_id", "projectId", default="") or ""
    )

    # Level 2: Target-task-scoped lane.
    target_task_id = _first(
        metadata,
        "target_task_id",
        "targetTaskId",
    ) or _first(
        delivery,
        "target_task_id",
        "targetTaskId",
    )
    if target_task_id is not None:
        return f"lane:{project_id}:task:{target_task_id}"

    # Level 3: Assignment-scoped lane.
    assignment_id = _first(
        metadata,
        "assignment_id",
        "targetAssignmentId",
        "target_assignment_id",
    ) or _first(
        delivery,
        "assignment_id",
        "targetAssignmentId",
        "target_assignment_id",
    )
    if assignment_id is not None:
        return f"lane:{project_id}:assignment:{assignment_id}"

    # Level 4: Worker-run-scoped lane.
    worker_run_id = _first(
        metadata,
        "worker_run_id",
        "workerRunId",
    ) or _first(
        delivery,
        "worker_run_id",
        "workerRunId",
    )
    if worker_run_id is not None:
        return f"lane:{project_id}:run:{worker_run_id}"

    # Level 5: No explicit lane; fall back to source channel.
    return None


def _remember_direct_agent_config(
    *,
    gateway_url: str = "",
    delivery_url: str = "",
    observation_url: str = "",
    channels_url: str = "",
    token: str | None = None,
    agent_identity: str = "",
) -> None:
    """Remember adapter config for module-level tool handlers.

    Hermes plugin tools are registered at module load time, while platform
    adapter config is supplied later when the gateway starts. Keep a
    process-local copy so tools can work from profile config without
    prompt-level env workarounds. Token values are never returned in output.
    """
    updates = {
        "gateway_url": (gateway_url or "").rstrip("/"),
        "delivery_url": (delivery_url or "").rstrip("/"),
        "observation_url": (observation_url or "").rstrip("/"),
        "channels_url": (channels_url or "").rstrip("/"),
        "agent_identity": (agent_identity or "").strip(),
    }
    if token:
        updates["token"] = token
    _DIRECT_AGENT_CONFIG_DEFAULTS.update({key: value for key, value in updates.items() if value})


class DenObservationClient:
    """Thin async HTTP client for posting agent_activity.v1 events to Observation.

    Posts ``agent_activity.v1`` payloads to ``POST /v1/observation/activity-events``
    (or the compatible ``/v1/observation/lifecycle-events`` alias).  Gracefully
    degrades on network/connectivity errors — logged at debug, never raises.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    @property
    def is_configured(self) -> bool:
        return bool(self._base_url)

    async def post_activity_event(
        self,
        source_domain: str,
        event_type: str,
        agent_identity: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        """Post an agent_activity.v1 event to observation.

        Logs and silently degrades on transport errors.  Never raises.
        """
        if not self.is_configured:
            return
        import httpx

        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        body = {
            "source_domain": source_domain,
            "event_type": event_type,
            "agent_identity": agent_identity,
            "payload": payload,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base_url}/v1/observation/activity-events",
                    json=body,
                    headers=headers,
                )
                resp.raise_for_status()
                logger.debug(
                    "[DenObservation] posted %s for %s (%s)",
                    event_type,
                    agent_identity.get("profile", "?"),
                    resp.status_code,
                )
        except Exception:
            logger.debug(
                "[DenObservation] failed to post %s for %s",
                event_type,
                agent_identity.get("profile", "?"),
                exc_info=True,
            )


class DenGatewayClient:
    """Small async HTTP client for Den Gateway delivery APIs."""

    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = 15.0):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token
        self.timeout = timeout

    async def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        import httpx

        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.request(method, f"{self.base_url}{path}", json=payload, headers=headers)
            response.raise_for_status()
            if not response.content:
                return {}
            return response.json()

    async def upsert_adapter_binding(self, payload: dict[str, Any]) -> Any:
        return await self._request("PUT", "/api/adapter-bindings/heartbeat", payload)

    async def claim_deliveries(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        result = await self._request("POST", "/api/deliveries/claim", payload)
        if isinstance(result, list):
            return result
        for key in ("deliveries", "claims", "items", "data"):
            items = result.get(key) if isinstance(result, dict) else None
            if isinstance(items, list):
                return items
        return []

    async def mark_delivered(self, delivery_request_id: int, payload: dict[str, Any]) -> Any:
        return await self._request("POST", f"/api/deliveries/{delivery_request_id}/delivered", payload)

    async def mark_completed(self, delivery_request_id: int, payload: dict[str, Any]) -> Any:
        """Mark a Gateway delivery request as completed/terminalized.

        Called after a successful final visible reply to terminalize the delivery
        with ack_kind (e.g. ``hermes_final_reply_posted``). Idempotent: Den
        Gateway ignores repeated complete calls for already-terminalized deliveries.
        """
        return await self._request("POST", f"/api/deliveries/{delivery_request_id}/complete", payload)

    async def mark_failed(self, delivery_request_id: int, payload: dict[str, Any]) -> Any:
        return await self._request("POST", f"/api/deliveries/{delivery_request_id}/fail", payload)


class _DirectAgentEventPoller:
    """Cursor-tracked poller for direct-agent wake events from Den Channels.

    Supports server-side cursor sync via subscription_ids (task #2554).
    The subscription_id maps a channel to a concrete subscription row on the
    server; after each poll the cursor is synced back so the server becomes
    the authoritative cursor store.
    """

    def __init__(
        self,
        channels_client: "DenChannelsClient",
        agent_identity: str,
        initial_after_ids: dict[int, int] | None = None,
        *,
        subscription_ids: dict[int, int] | None = None,
        sync_cursors_to_server: bool = True,
    ):
        self._channels_client = channels_client
        self._agent_identity = agent_identity
        # subscription_ids maps channel_id -> subscription_id for server-side cursor sync
        self._subscription_ids: dict[int, int] = dict(subscription_ids or {})
        self._sync_cursors_to_server = sync_cursors_to_server
        self._cursor_state_path = os.path.join(
            os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes"),
            "state",
            "den_channels_event_cursors.json",
        )
        self._cursor_by_channel: dict[int, int] = self._load_cursors()
        init_skipped: list[str] = []
        for channel_id, after_id in dict(initial_after_ids or {}).items():
            # Treat config as a floor, not as an override of a newer persisted cursor.
            current = self._cursor_by_channel.get(channel_id, 0)
            if after_id > current:
                self._cursor_by_channel[channel_id] = after_id
            elif after_id and after_id <= current:
                init_skipped.append(f"ch{channel_id}:cfg={after_id} < persisted={current}")
        self._save_cursors()
        cursors_log = {str(k): v for k, v in sorted(self._cursor_by_channel.items())}
        if cursors_log:
            logger.info(
                "[DenChannels] direct-agent event cursors initialized: %s",
                json.dumps(cursors_log),
            )
        if init_skipped:
            logger.info(
                "[DenChannels] config initial_after_ids overridden by newer persisted cursors: %s",
                "; ".join(init_skipped),
            )

    def _load_cursors(self) -> dict[int, int]:
        try:
            with open(self._cursor_state_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return {}
        except Exception:
            logger.exception("[DenChannels] Failed to load direct-agent event cursor file")
            return {}
        cursors = payload.get("cursors") if isinstance(payload, dict) else None
        if not isinstance(cursors, dict):
            return {}
        loaded: dict[int, int] = {}
        for raw_channel_id, raw_after_id in cursors.items():
            channel_id = _coerce_int(raw_channel_id)
            after_id = _coerce_int(raw_after_id)
            if channel_id is not None and after_id is not None:
                loaded[channel_id] = after_id
        return loaded

    def _save_cursors(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._cursor_state_path), exist_ok=True)
            payload = {
                "agentIdentity": self._agent_identity,
                "cursors": {str(k): v for k, v in sorted(self._cursor_by_channel.items())},
            }
            tmp_path = f"{self._cursor_state_path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(tmp_path, self._cursor_state_path)
        except Exception:
            logger.exception("[DenChannels] Failed to save direct-agent event cursor file")

    async def poll(self, channel_id: int, limit: int = 10) -> list[dict[str, Any]]:
        after_id = self._cursor_by_channel.get(channel_id, 0)
        page = await self._channels_client.get_direct_agent_events(
            channel_id=channel_id, after_id=after_id, limit=limit
        )
        items = page.get("items") or page.get("eventItems") or []
        my_events: list[dict[str, Any]] = []
        cursor_changed = False
        skipped_stale = 0
        for event in items:
            event_id = _coerce_int(event.get("id") or event.get("eventId"))
            if event_id is None:
                continue
            if event_id <= after_id:
                skipped_stale += 1
                continue
            if self._is_targeting_me(event):
                my_events.append(event)
            if event_id > self._cursor_by_channel.get(channel_id, 0):
                self._cursor_by_channel[channel_id] = event_id
                cursor_changed = True
        next_after = _coerce_int(page.get("nextAfterId") or page.get("next_after_id"))
        if next_after is not None:
            current = self._cursor_by_channel.get(channel_id, 0)
            if next_after > current:
                self._cursor_by_channel[channel_id] = next_after
                cursor_changed = True
        if cursor_changed:
            self._save_cursors()
            if self._sync_cursors_to_server:
                sub_id = self._subscription_ids.get(channel_id)
                if sub_id is not None:
                    try:
                        import asyncio
                        asyncio.ensure_future(
                            self._channels_client.upsert_subscription_cursor(
                                subscription_id=sub_id,
                                last_seen_id=self._cursor_by_channel.get(channel_id, 0),
                            )
                        )
                    except Exception:
                        logger.debug(
                            "[DenChannels] failed to sync cursor for ch%d sub%d",
                            channel_id, sub_id,
                        )
        if skipped_stale or (items and not my_events):
            logger.debug(
                "[DenChannels] ch%d poll: cursor=%d, returned=%d, stale_skipped=%d, total=%d",
                channel_id, self._cursor_by_channel.get(channel_id, 0),
                len(my_events), skipped_stale, len(items),
            )
        return my_events

    def _is_targeting_me(self, event: dict[str, Any]) -> bool:
        member_id = str(event.get("memberIdentity") or event.get("member_identity") or "").strip()
        if member_id:
            return member_id == self._agent_identity
        source_id = str(event.get("sourceId") or event.get("source_id") or event.get("SourceId") or "")
        if source_id.startswith("direct-agent-message:"):
            parts = source_id.split(":")
            if len(parts) >= 3:
                return parts[2] == self._agent_identity
        return False

    def update_subscription_mapping(self, subscription_id: int, channel_id: int) -> None:
        """Update the channel -> subscription_id mapping at runtime.
        Called when channels are discovered via subscriptions."""
        self._subscription_ids[channel_id] = subscription_id

    def remove_subscription_mapping(self, channel_id: int) -> None:
        """Remove a channel from the subscription mapping.
        Called when a subscription is released."""
        self._subscription_ids.pop(channel_id, None)

    async def initialize_cursors_from_server(self) -> None:
        """Fetch cursors from the server for all known subscription IDs.
        Only advances cursors (never rewinds), consistent with the floor semantics."""
        if not self._sync_cursors_to_server:
            return
        for channel_id, sub_id in list(self._subscription_ids.items()):
            try:
                cursors = await self._channels_client.list_subscription_cursors(
                    subscription_id=sub_id
                )
                if isinstance(cursors, list):
                    for cursor in cursors:
                        stream = cursor.get("streamKind") or cursor.get("stream_kind") or ""
                        if stream != "direct_agent_events" and stream != "":
                            continue
                        server_last_seen = _coerce_int(
                            cursor.get("lastSeenId") or cursor.get("last_seen_id") or 0
                        ) or 0
                        current = self._cursor_by_channel.get(channel_id, 0)
                        if server_last_seen > current:
                            self._cursor_by_channel[channel_id] = server_last_seen
                            logger.info(
                                "[DenChannels] cursor advanced from server: ch%d sub%d %d -> %d",
                                channel_id, sub_id, current, server_last_seen,
                            )
            except Exception:
                logger.debug(
                    "[DenChannels] failed to init cursor from server for ch%d sub%d",
                    channel_id, sub_id,
                )
        self._save_cursors()


class DenChannelsClient:
    """Small async HTTP client for Den Channels message APIs."""

    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = 15.0):
        self.base_url = (base_url or "").rstrip("/")
        self.token = token
        self.timeout = timeout

    async def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        import httpx

        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.request(method, f"{self.base_url}{path}", json=payload, headers=headers)
            response.raise_for_status()
            if not response.content:
                return {}
            return response.json()

    async def get_direct_agent_events(
        self, *, channel_id: int, after_id: int = 0, limit: int = 10
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/api/direct-agent-events?channelId={channel_id}&afterId={after_id}&limit={limit}",
        )

    async def get_channel_memberships(
        self,
        *,
        member_identity: str,
        include_left: bool = False,
        include_ordinary_memberships: bool = True,
        limit: int = 200,
    ) -> dict[str, Any]:
        from urllib.parse import urlencode

        query = urlencode({
            "memberIdentity": member_identity,
            "includeLeft": str(include_left).lower(),
            "includeOrdinaryMemberships": str(include_ordinary_memberships).lower(),
            "limit": limit,
        })
        return await self._request("GET", f"/api/channel-memberships?{query}")

    async def get_message_readback(self, message_id: str | int) -> dict[str, Any]:
        """Readback only: fetch a single channel message by ID.

        ``TODO(#2786)``: This calls the Gateway-shaped readback route because
        Den Channels does not yet expose a standalone ``GET /api/messages/{id}``
        route outside the Gateway compatibility prefix.  Once Channels has a
        native non-gateway single-message route, this method should be switched.
        This is a read-only compatibility call — no new actuation depends on it.
        """
        return await self._request("GET", f"/api/gateway/messages/{message_id}")

    async def post_channel_message(self, channel_id: str | int, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", f"/api/channels/{channel_id}/messages", payload)

    async def add_reaction(self, message_id: str | int, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", f"/api/channel-messages/{message_id}/reactions", payload)

    # ------------------------------------------------------------------
    # Subscription API methods (task #2554)
    # ------------------------------------------------------------------

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
        """Discover active subscriptions for a member and/or profile."""
        from urllib.parse import urlencode
        params = {"memberIdentity": member_identity, "limit": str(limit)}
        if profile_identity:
            params["profileIdentity"] = profile_identity
        if channel_id is not None:
            params["channelId"] = str(channel_id)
        if subscription_purpose:
            params["subscriptionPurpose"] = subscription_purpose
        if include_inactive:
            params["includeInactive"] = "true"
        query = urlencode(params)
        return await self._request("GET", f"/api/channel-subscriptions?{query}")

    async def upsert_subscription_cursor(
        self,
        *,
        subscription_id: int,
        stream_kind: str = "direct_agent_events",
        last_seen_id: int,
        cursor_json: str | None = None,
    ) -> dict[str, Any]:
        """Persist a subscription cursor (poll position) on the server."""
        payload: dict[str, Any] = {
            "subscriptionId": subscription_id,
            "streamKind": stream_kind,
            "lastSeenId": last_seen_id,
        }
        if cursor_json is not None:
            payload["cursorJson"] = cursor_json
        return await self._request("PUT", "/api/channel-subscription-cursors", payload)

    async def list_subscription_cursors(
        self,
        *,
        subscription_id: int,
    ) -> list[dict[str, Any]]:
        """Get subscription cursors from the server for a subscription."""
        return await self._request(
            "GET", f"/api/channel-subscriptions/{subscription_id}/cursors"
        )


class DenChannelsAdapter(BasePlatformAdapter):
    """Den Channels platform adapter for long-running Hermes Gateway sessions."""

    # Den Channels is append-only from the Hermes gateway's point of view: the
    # durable transcript should receive one terminal gateway_delivery message at
    # the end of a delivery, while tool/status/interim text is represented as
    # nonterminal activity/progress.  Do not let Hermes' generic token-streaming
    # consumer create/edit normal channel messages, because its first pre-tool
    # assistant chunk can otherwise be mistaken for the terminal reply and make
    # the real post-tool answer get suppressed by the gateway duplicate-send
    # guard.
    SUPPORTS_MESSAGE_EDITING = False

    def __init__(
        self,
        config: PlatformConfig,
        *,
        gateway_client: Any | None = None,
        channels_client: Any | None = None,
        sleep: Any | None = None,
    ) -> None:
        super().__init__(config, Platform(_PLATFORM_NAME))
        extra = _extra(config)
        if isinstance(getattr(config, "extra", None), dict):
            # Den Channels project/channel lanes are shared planning lanes by
            # design.  Force the platform-local default even when the gateway's
            # global group default is per-user isolation for public chat apps.
            config.extra["group_sessions_per_user"] = False
            config.extra.setdefault("thread_sessions_per_user", False)
        self.gateway_url = str(extra.get("gateway_url") or os.getenv("DEN_GATEWAY_URL") or "").rstrip("/")
        self.channels_url = str(extra.get("channels_url") or os.getenv("DEN_CHANNELS_URL") or "").rstrip("/")
        self.delivery_url = str(
            extra.get("delivery_url")
            or extra.get("DELIVERY_URL")
            or os.getenv("DEN_DELIVERY_URL")
            or self.gateway_url
            or ""
        ).rstrip("/")
        self.observation_url = str(
            extra.get("observation_url")
            or extra.get("OBSERVATION_URL")
            or os.getenv("DEN_OBSERVATION_URL")
            or self.channels_url
            or ""
        ).rstrip("/")
        self.project_id = str(extra.get("project_id") or os.getenv("DEN_CHANNELS_PROJECT_ID") or "").strip()
        self.agent_identity = str(extra.get("agent_identity") or os.getenv("HERMES_AGENT_IDENTITY") or os.getenv("HERMES_PROFILE") or "hermes").strip()
        self.role = str(extra.get("role") or os.getenv("HERMES_AGENT_ROLE") or "agent").strip()
        self.profile = str(extra.get("profile") or os.getenv("HERMES_PROFILE") or self.agent_identity).strip()
        self.adapter_instance_id = str(
            extra.get("adapter_instance_id")
            or os.getenv("DEN_CHANNELS_ADAPTER_INSTANCE_ID")
            or f"{socket.gethostname()}:{self.profile}:{self.role}:gateway"
        )
        self.pool_member_id = str(
            extra.get("pool_member_id")
            or os.getenv("DEN_HERMES_POOL_MEMBER_ID")
            or ""
        ).strip() or None
        self.agent_instance_id = str(
            extra.get("agent_instance_id")
            or os.getenv("DEN_HERMES_AGENT_INSTANCE_ID")
            or os.getenv("DEN_CHANNELS_AGENT_INSTANCE_ID")
            or (self.adapter_instance_id if self.pool_member_id else "")
            or ""
        ).strip() or None
        self.claim_interval_seconds = float(extra.get("claim_interval_seconds") or 2.0)
        self.claim_limit = max(1, _coerce_int(extra.get("claim_limit")) or 1)
        self.lease_seconds = max(1, _coerce_int(extra.get("lease_seconds")) or 300)
        self.start_claim_loop = _coerce_bool(extra.get("start_claim_loop"), bool(self.gateway_url))
        self.poll_interval_seconds = float(extra.get("poll_interval_seconds") or 2.0)
        self.poll_limit = max(1, _coerce_int(extra.get("poll_limit")) or 10)
        self.start_poll_loop = _coerce_bool(extra.get("start_poll_loop"), True)
        self.poll_channel_ids = _coerce_int_list(
            extra.get("poll_channel_ids")
            or extra.get("channel_ids")
            or extra.get("channel_id")
            or os.getenv("DEN_CHANNELS_POLL_CHANNEL_IDS")
            or os.getenv("DEN_CHANNELS_CHANNEL_IDS")
            or os.getenv("DEN_CHANNELS_CHANNEL_ID")
        )
        self.poll_initial_after_ids = _coerce_int_mapping(
            extra.get("poll_initial_after_ids")
            or extra.get("poll_initial_after_id_by_channel")
            or os.getenv("DEN_CHANNELS_POLL_INITIAL_AFTER_IDS")
        )
        self._sleep = sleep or asyncio.sleep
        token = str(extra.get("token") or os.getenv("DEN_GATEWAY_TOKEN") or "").strip() or None
        channels_token = str(extra.get("channels_token") or os.getenv("DEN_CHANNELS_TOKEN") or token or "").strip() or None
        observation_token = str(
            extra.get("observation_token")
            or os.getenv("DEN_OBSERVATION_TOKEN")
            or channels_token
            or token
            or ""
        ).strip() or None
        _remember_direct_agent_config(
            gateway_url=self.gateway_url,
            delivery_url=self.delivery_url,
            observation_url=self.observation_url,
            channels_url=self.channels_url,
            token=channels_token or token,
            agent_identity=self.agent_identity,
        )
        self._has_trusted_transport = bool(
            token or channels_token or _is_private_url(self.gateway_url) or _is_private_url(self.channels_url)
        )
        self.gateway_client = gateway_client or (DenGatewayClient(self.gateway_url, token=token) if self.gateway_url else None)
        self.channels_client = channels_client or DenChannelsClient(self.channels_url, token=channels_token)
        self.observation_client = DenObservationClient(
            self.observation_url,
            token=observation_token,
        )
        self._claim_task: asyncio.Task | None = None
        self._event_task: asyncio.Task | None = None
        self._sync_cursors_to_server = _coerce_bool(
            extra.get("sync_cursors_to_server"), True
        )
        self._event_poller = _DirectAgentEventPoller(
            self.channels_client,
            self.agent_identity,
            self.poll_initial_after_ids,
            subscription_ids=None,  # populated during subscription discovery
            sync_cursors_to_server=self._sync_cursors_to_server,
        )
        self._polled_channels: set[int] = set()
        self._last_channel_discovery = 0.0
        self._channel_discovery_interval = float(extra.get("channel_discovery_interval_seconds") or 30.0)
        # Subscription-based channel discovery (task #2554)
        # Maps subscription_id -> channel_id for cursor sync with server.
        self._subscription_cache: dict[int, int] = {}
        self._last_subscription_discovery = 0.0
        self._subscription_discovery_ever_ran = False
        self._subscription_discovery_interval = float(
            extra.get("subscription_discovery_interval_seconds") or 60.0
        )
        self._contexts_by_session: dict[str, _DeliveryContext] = {}
        self._contexts_by_chat: dict[str, _DeliveryContext] = {}
        self._contexts_by_lane: dict[tuple[str, str | None], _DeliveryContext] = {}
        self._contexts_by_delivery_id: dict[int, _DeliveryContext] = {}
        self._contexts_by_task: dict[asyncio.Task, _DeliveryContext] = {}
        self._terminal_delivery_ids: set[int] = set()

    @property
    def name(self) -> str:
        return "Den Channels"

    async def connect(self) -> bool:
        if not self.adapter_instance_id or not self.agent_identity:
            self._set_fatal_error("den_channels_config", "Den Channels adapter identity is missing", retryable=False)
            return False
        if not self._has_trusted_transport:
            self._set_fatal_error(
                "den_channels_auth",
                "Den Channels adapter requires DEN_GATEWAY_TOKEN/token or a private/loopback DEN_GATEWAY_URL",
                retryable=False,
            )
            return False
        if self.gateway_client is not None:
            await self.gateway_client.upsert_adapter_binding(self._binding_payload())
        self._running = True
        if self.start_claim_loop and self.gateway_client is not None:
            try:
                self._claim_task = asyncio.create_task(self._claim_loop())
            except RuntimeError:
                # Unit tests may construct without a running loop; connect still proves config/binding.
                self._claim_task = None
        if self.start_poll_loop and self.channels_url:
            try:
                self._event_task = asyncio.create_task(self._event_poll_loop())
            except RuntimeError:
                self._event_task = None

        # Initialize subscriptions and cursors from the server (task #2554).
        # This runs on connect so newly added channels are discovered immediately
        # without waiting for the first poll loop cycle.
        if self.channels_url and self.agent_identity:
            try:
                await self._discover_and_sync_subscriptions()
            except Exception:
                logger.debug("[DenChannels] subscription init failed on connect", exc_info=True)

        await self._emit_observation_activity(
            "adapter_connected",
            summary=f"Hermes {self.profile} connected to Den Channels.",
            surface="channel",
        )
        return True

    async def disconnect(self) -> None:
        self._running = False
        await self._emit_observation_activity(
            "adapter_disconnected",
            severity="warning",
            summary=f"Hermes {self.profile} disconnected from Den Channels.",
            reason_code="graceful_shutdown",
        )
        if self._claim_task and not self._claim_task.done():
            self._claim_task.cancel()
            try:
                await self._claim_task
            except asyncio.CancelledError:
                pass
        self._claim_task = None
        if self._event_task and not self._event_task.done():
            self._event_task.cancel()
            try:
                await self._event_task
            except asyncio.CancelledError:
                pass
        self._event_task = None

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"id": chat_id, "name": chat_id, "type": "channel"}

    async def react_to_message(self, message_id: str | int, reaction_key: str) -> dict[str, Any]:
        """Add a bounded non-pulsing Den Channels reaction as this agent.

        Reactions require an explicit Den Channels message id and emoji/key. They
        create channel_reactions rows only; they do not post channel_messages and
        therefore do not trigger Gateway wake fan-out.
        """
        message_int = _coerce_int(message_id)
        reaction_text = str(reaction_key or "").strip()
        if message_int is None or message_int <= 0:
            raise ValueError("message_id must be a positive Den Channels message id")
        if not reaction_text:
            raise ValueError("reaction_key is required")
        return await self.channels_client.add_reaction(message_int, {
            "reactorType": "agent",
            "reactorIdentity": self.agent_identity,
            "reactionKey": reaction_text,
        })

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        context = self._context_for_send(chat_id, metadata)
        if context is None:
            return SendResult(success=False, error="No Den Channels delivery context for reply")

        metadata = metadata or {}
        raw_delivery_stage = metadata.get("delivery_stage")
        if raw_delivery_stage is None and context is not None and metadata.get("notify") is not True:
            # Hermes can emit assistant text in the same model response as
            # tool_calls. The gateway surfaces that through the interim
            # assistant callback with either explicit delivery metadata or only
            # lane/thread metadata.  _context_for_send() resolves both shapes to
            # a Den delivery context, but only the true post-agent response gets
            # the final-response notify marker from BasePlatformAdapter.  Keep
            # unmarked sends nonterminal so they cannot consume
            # ``gateway-delivery:<id>:final`` or mark the Den Gateway delivery
            # delivered before tools finish.
            delivery_stage = "interim"
        else:
            delivery_stage = str(raw_delivery_stage or "final").strip().lower()
        is_terminal_reply = delivery_stage in {"", "final"}
        dedupe_suffix = "final"
        completion_status = "reply_posted"
        if not is_terminal_reply:
            safe_stage = "approval" if delivery_stage == "approval_prompt" else "".join(
                ch if ch.isalnum() or ch in {"_", "-"} else "_"
                for ch in delivery_stage
            )
            dedupe_suffix = f"{safe_stage}:{context.attempt_id or 'unknown'}"
            completion_status = delivery_stage

        payload: dict[str, Any] = {
            "senderType": "agent",
            "senderIdentity": self.agent_identity,
            "body": content,
            "messageKind": "agent_text",
            "sourceKind": "gateway_delivery",
            "sourceId": str(context.delivery_request_id),
            "sourceProjectId": context.project_id,
            "dedupeKey": f"gateway-delivery:{context.delivery_request_id}:{dedupe_suffix}",
            "metadataJson": json.dumps(
                {
                    "delivery_request_id": context.delivery_request_id,
                    "attempt_id": context.attempt_id,
                    "adapter_instance_id": self.adapter_instance_id,
                    "session_key": context.session_key,
                    "session_id": context.session_id,
                    "original_source_kind": context.original_source_kind,
                    "original_source_id": context.original_source_id,
                    "completion_status": completion_status,
                    "delivery_stage": delivery_stage,
                    "terminal_delivery": is_terminal_reply,
                },
                sort_keys=True,
            ),
        }
        reply_anchor = _coerce_int(reply_to) or context.trigger_message_id
        if reply_anchor is not None:
            payload["replyToMessageId"] = reply_anchor
        if context.thread_root_message_id is not None:
            payload["threadRootMessageId"] = context.thread_root_message_id

        try:
            posted = await self.channels_client.post_channel_message(context.channel_id, payload)
            message_id = _first(posted if isinstance(posted, dict) else {}, "id", "messageId", "channel_message_id")
            message_int = _coerce_int(message_id)
            if not is_terminal_reply:
                return SendResult(success=True, message_id=str(message_id), raw_response=posted)
            completed_payload = {
                "attempt_id": context.attempt_id,
                "adapter_kind": "hermes_profile",
                "adapter_instance_id": self.adapter_instance_id,
                "external_message_id": str(message_int or message_id),
                "session_id": context.session_id,
                "ack_kind": "hermes_final_reply_posted",
                "metadata_json": json.dumps(
                    {
                        "channel_message_id": message_int or message_id,
                        "delivery_request_id": context.delivery_request_id,
                        "source_kind": "gateway_delivery",
                    },
                    sort_keys=True,
                    default=str,
                ),
            }
            if self.gateway_client is None:
                # Channels-owned direct-agent polling does not have a legacy
                # Gateway delivery lifecycle endpoint to complete. The visible
                # gateway_delivery reply itself is the terminal Channels evidence.
                self._terminal_delivery_ids.add(context.delivery_request_id)
            else:
                try:
                    await self.gateway_client.mark_completed(context.delivery_request_id, completed_payload)
                    self._terminal_delivery_ids.add(context.delivery_request_id)
                except Exception:
                    logger.warning(
                        "[DenChannels] posted reply for delivery %s but failed to mark completed",
                        context.delivery_request_id,
                        exc_info=True,
                    )
            return SendResult(success=True, message_id=str(message_id), raw_response=posted)
        except Exception as exc:
            if is_terminal_reply:
                await self._mark_failed(context, "reply_post_failed", exc)
            return SendResult(success=False, error=str(exc))
        finally:
            if is_terminal_reply:
                self._clear_context(context)

    async def delivery_to_event(self, delivery: dict[str, Any]) -> MessageEvent:
        delivery = dict(delivery or {})
        delivery_id = _coerce_int(_first(delivery, "delivery_request_id", "deliveryRequestId", "id"))
        if delivery_id is None:
            raise ValueError("Den Gateway delivery is missing delivery_request_id")
        attempt_id = _coerce_int(_first(delivery, "attempt_id", "attemptId"))
        project_id = str(_first(delivery, "project_id", "projectId", default=self.project_id) or self.project_id)
        source_id = str(_first(delivery, "source_id", "sourceId", default="") or "")
        source_kind = str(_first(delivery, "source_kind", "sourceKind", default="") or "")
        metadata = _json_obj(_first(delivery, "metadata_json", "metadataJson", "metadata", default={}))
        message: dict[str, Any] = {}
        if source_id and source_kind in {"channel_message", "channelMessage", "message", ""}:
            message = await self.channels_client.get_message_readback(source_id)
            if not isinstance(message, dict):
                message = {}

        channel_id = _coerce_int(
            _first(metadata, "channel_id", "channelId", default=_first(message, "channelId", "channel_id"))
        )
        if channel_id is None:
            raise ValueError("Den Channels delivery is missing channel_id")
        thread_root = _coerce_int(
            _first(metadata, "thread_root_message_id", "threadRootMessageId", default=_first(message, "threadRootMessageId", "thread_root_message_id"))
        )
        trigger_message_id = _coerce_int(_first(message, "id", "messageId", default=source_id))
        sender = str(
            _first(metadata, "sender_identity", "senderIdentity", default=_first(message, "senderIdentity", "sender_identity", default="unknown"))
            or "unknown"
        )
        body = str(_first(
            message,
            "body",
            "text",
            "content",
            default=_first(delivery, "body", "text", "content", "context_summary", "contextSummary", default=""),
        ) or "")
        chat_name = _first(metadata, "channel_slug", "channelSlug", "channel_name", "channelName")

        # --- Session owner / lane selection (#1890/#1871) ---
        # Prefer durable session-owner identity when available; source/channel
        # lanes remain as compatibility metadata or when session_scope=source_lane.
        session_owner_id = _resolve_session_owner(
            delivery, metadata, self.adapter_instance_id, self.pool_member_id, self.agent_instance_id, self.profile
        )
        conversation_lane_id = _resolve_conversation_lane(delivery, metadata)
        raw_chat_id = f"project:{project_id}:channel:{channel_id}"

        if session_owner_id is not None:
            chat_type = "thread" if thread_root is not None else "channel"
            chat_id = session_owner_id
            thread_id = f"thread:{thread_root}" if thread_root is not None else None
        elif conversation_lane_id is not None:
            # Use the explicit lane id as the Hermes session key namespace.
            # Thread qualification is preserved only when the explicit lane
            # does not already embed thread context.
            chat_type = "thread" if thread_root is not None else "channel"
            chat_id = conversation_lane_id
            thread_id = f"thread:{thread_root}" if thread_root is not None else None
        else:
            chat_type = "thread" if thread_root is not None else "channel"
            chat_id = raw_chat_id
            thread_id = f"thread:{thread_root}" if thread_root is not None else None

        # user_id is intentionally omitted: sender identity is separate from
        # lane identity.  user_name is set for display prefix only.
        source = self.build_source(
            chat_id=chat_id,
            chat_name=str(chat_name) if chat_name else None,
            chat_type=chat_type,
            user_name=sender,
            thread_id=thread_id,
            message_id=str(trigger_message_id or source_id),
        )
        event = MessageEvent(
            text=body,
            message_type=MessageType.TEXT,
            source=source,
            raw_message={
                **delivery,
                "delivery_request_id": delivery_id,
                "attempt_id": attempt_id,
                "metadata": metadata,
                "channel_message": message,
                "channel_id": channel_id,
                "project_id": project_id,
                "conversation_lane_id": conversation_lane_id,
                "session_owner_id": session_owner_id,
                "raw_chat_id": raw_chat_id,
            },
            message_id=str(trigger_message_id or source_id),
            reply_to_message_id=str(thread_root) if thread_root is not None else None,
            internal=True,
        )
        self.set_delivery_context(event, bind_current_task=False)
        return event

    def set_delivery_context(self, event: MessageEvent, *, bind_current_task: bool = True) -> None:
        context = self._build_context(event)
        if context is None:
            return
        self._contexts_by_session[context.session_key] = context
        self._contexts_by_chat[str(event.source.chat_id)] = context
        self._contexts_by_lane[self._lane_key(event.source.chat_id, event.source.thread_id)] = context
        self._contexts_by_delivery_id[context.delivery_request_id] = context
        if bind_current_task:
            task = asyncio.current_task()
            if task is not None:
                self._contexts_by_task[task] = context

    def _build_context(self, event: MessageEvent) -> _DeliveryContext | None:
        raw = event.raw_message if isinstance(event.raw_message, dict) else {}
        delivery_id = _coerce_int(_first(raw, "delivery_request_id", "deliveryRequestId", "id"))
        if delivery_id is None:
            return None
        raw_message = raw.get("channel_message")
        message: dict[str, Any] = raw_message if isinstance(raw_message, dict) else {}
        channel_id = _coerce_int(_first(raw, "channel_id", "channelId", default=_first(message, "channelId", "channel_id")))
        if channel_id is None:
            return None
        trigger_message_id = _coerce_int(_first(message, "id", "messageId", default=getattr(event, "message_id", None)))
        thread_root = _coerce_int(_first(message, "threadRootMessageId", "thread_root_message_id"))
        conversation_lane_id = raw.get("conversation_lane_id")
        session_owner_id = raw.get("session_owner_id")
        return _DeliveryContext(
            delivery_request_id=delivery_id,
            attempt_id=_coerce_int(_first(raw, "attempt_id", "attemptId")),
            project_id=str(_first(raw, "project_id", "projectId", default=self.project_id) or self.project_id),
            channel_id=channel_id,
            trigger_message_id=trigger_message_id,
            thread_root_message_id=thread_root,
            session_key=build_session_key(event.source, group_sessions_per_user=False),
            session_id=str(_first(raw, "session_id", "sessionId", default="") or "") or None,
            original_source_kind=str(_first(raw, "source_kind", "sourceKind", default="") or ""),
            original_source_id=str(_first(raw, "source_id", "sourceId", default="") or ""),
            raw_delivery=raw,
            conversation_lane_id=str(conversation_lane_id) if conversation_lane_id is not None else None,
            session_owner_id=str(session_owner_id) if session_owner_id is not None else None,
        )

    async def on_processing_start(self, event: MessageEvent) -> None:
        self.set_delivery_context(event)
        context = self._context_for_event(event)
        if context is not None:
            self._set_activity_environment(context)
            logger.info(
                "[DenChannels] processing delivery %d on ch%d session=%s",
                context.delivery_request_id,
                context.channel_id,
                context.session_key,
            )
            await self._emit_observation_activity(
                "work_started",
                summary=f"Hermes {self.profile} processing delivery {context.delivery_request_id}.",
                visibility="agent",
                surface="worker",
                session_key=context.session_key,
                work_ref={
                    "project_id": context.project_id,
                    "task_id": _coerce_int(
                        _first(context.raw_delivery or {}, "task_id", "taskId")
                    ),
                    "channel_id": context.channel_id,
                },
            )

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        task = asyncio.current_task()
        context = self._contexts_by_task.get(task) if task is not None else None
        try:
            if getattr(outcome, "value", outcome) == ProcessingOutcome.SUCCESS.value:
                # A claimed Den delivery needs a terminal Den state.  If the
                # Gateway handler returned no visible response, BasePlatformAdapter
                # classifies local processing as success, but there was no
                # gateway_delivery final message and therefore nothing to mark
                # delivered.  Fail the claim explicitly so Den can retry/escalate
                # instead of leaving it claimed forever.
                if context is not None:
                    await self._mark_failed(context, "processing_no_response", event.raw_message)
                    await self._emit_observation_activity(
                        "work_failed",
                        severity="error",
                        summary=f"Hermes {self.profile} processing {context.delivery_request_id}: no response.",
                        reason_code="processing_no_response",
                        visibility="agent",
                        surface="worker",
                        session_key=context.session_key,
                        work_ref={
                            "project_id": context.project_id,
                            "channel_id": context.channel_id,
                        } if context else None,
                    )
                    self._clear_context(context)
                return
            context = self._build_context(event) or context or self._context_for_event(event)
            if context is not None:
                await self._mark_failed(context, f"processing_{getattr(outcome, 'value', outcome)}", event.raw_message)
                await self._emit_observation_activity(
                    "work_failed",
                    severity="error",
                    summary=f"Hermes {self.profile} processing {context.delivery_request_id}: {getattr(outcome, 'value', outcome)}.",
                    reason_code=f"processing_{getattr(outcome, 'value', outcome)}",
                    visibility="agent",
                    surface="worker",
                    work_ref={
                        "project_id": context.project_id,
                        "channel_id": context.channel_id,
                    },
                )
                self._clear_context(context)
        finally:
            self._clear_activity_environment(context)

    def _build_observation_identity(self) -> dict[str, Any]:
        """Build the agent_identity dict for observation events."""
        identity: dict[str, Any] = {
            "profile": self.profile or self.agent_identity,
        }
        if self.agent_instance_id:
            identity["instance_id"] = self.agent_instance_id
        elif self.adapter_instance_id:
            identity["instance_id"] = self.adapter_instance_id
        return identity

    async def _emit_observation_activity(
        self,
        event_type: str,
        *,
        source_domain: str = "runtime",
        summary: str = "",
        severity: str = "info",
        visibility: str = "agent",
        surface: str = "worker",
        session_key: str | None = None,
        work_ref: dict[str, Any] | None = None,
        result_ref: dict[str, Any] | None = None,
        reason_code: str | None = None,
        tool_name: str | None = None,
    ) -> None:
        """Post an agent_activity.v1 event to observation.

        Async so callers (connect, disconnect, processing lifecycle) can await it.
        All observation write failures are logged at debug and silently degraded.
        """
        if not self.observation_client.is_configured:
            return
        payload: dict[str, Any] = {
            "kind": "agent_activity.v1",
            "schema_version": 1,
            "summary": (summary or "")[:240],
            "severity": severity,
            "visibility": visibility,
            "adapter": "hermes",
            "surface": surface,
        }
        if session_key:
            payload["session_key"] = session_key
        if work_ref:
            payload["work_ref"] = work_ref
        if result_ref:
            payload["result_ref"] = result_ref
        if reason_code:
            payload["reason_code"] = reason_code
        if tool_name:
            payload["tool_name"] = tool_name

        await self.observation_client.post_activity_event(
            source_domain=source_domain,
            event_type=event_type,
            agent_identity=self._build_observation_identity(),
            payload=payload,
        )

    def _set_activity_environment(self, context: _DeliveryContext) -> None:
        payload = {
            "gatewayUrl": self.gateway_url,
            "deliveryUrl": self.delivery_url,
            "observationUrl": self.observation_url,
            "channelsUrl": self.channels_url,
            "token": self.gateway_client.token if isinstance(self.gateway_client, DenGatewayClient) else None,
            "channelId": context.channel_id,
            "projectId": context.project_id,
            "agentIdentity": self.agent_identity,
            "deliveryRequestId": context.delivery_request_id,
            "hermesSessionKey": context.session_key,
            "taskId": _coerce_int(_first(context.raw_delivery, "task_id", "taskId")),
            "threadId": _coerce_int(_first(context.raw_delivery, "thread_id", "threadId")),
            "anchorMessageId": context.trigger_message_id,
        }
        # Pool member identity for concrete slot targeting.
        if self.pool_member_id:
            payload["poolMemberId"] = self.pool_member_id
        # Profile identity distinguishes the concrete adapter instance from
        # the generic profile name (useful when multiple pool members share a
        # single spawned-coder profile but have distinct slot identities).
        payload["profileIdentity"] = self.profile
        if self.agent_instance_id:
            payload["agentInstanceId"] = self.agent_instance_id
        # Merge the parsed metadata dict so target-work fields nested inside
        # delivery metadata_json are visible alongside top-level delivery keys.
        raw_meta = context.raw_delivery.get("metadata")
        merged_source: dict[str, Any] = dict(context.raw_delivery)
        if isinstance(raw_meta, dict):
            for key, value in raw_meta.items():
                if key not in merged_source or merged_source[key] is None:
                    merged_source[key] = value
        # Forward delivery target-work metadata so the worker-visible activity
        # context can report concrete slot identity and target work.
        for src_key, dst_key in (
            ("pool_member_id", "poolMemberId"),
            ("poolMemberId", "poolMemberId"),
            ("agent_instance_id", "agentInstanceId"),
            ("agentInstanceId", "agentInstanceId"),
            ("worker_run_id", "workerRunId"),
            ("workerRunId", "workerRunId"),
            ("worker_role", "workerRole"),
            ("workerRole", "workerRole"),
            ("assignment_id", "assignmentId"),
            ("targetAssignmentId", "assignmentId"),
            ("target_assignment_id", "assignmentId"),
        ):
            value = _first(merged_source, src_key)
            if value is not None:
                payload[dst_key] = value
        if context.conversation_lane_id is not None:
            payload["conversationLaneId"] = context.conversation_lane_id
        _ACTIVITY_CONTEXT_VAR.set(payload)
        with _ACTIVITY_LOCK:
            _ACTIVITY_STATES.pop(_activity_state_key(payload), None)

    def _clear_activity_environment(self, context: _DeliveryContext | None) -> None:
        if context is not None:
            with _ACTIVITY_LOCK:
                _ACTIVITY_STATES.pop(str(context.delivery_request_id), None)
        _ACTIVITY_CONTEXT_VAR.set({})
        os.environ.pop(_ACTIVITY_CONTEXT_ENV, None)

    def _binding_payload(self) -> dict[str, Any]:
        capabilities = {
            "accepted_delivery_modes": ["wake", "notify"],
            "durable_sessions": True,
            "status_events": True,
            "source_kind": "gateway_delivery",
            "platform": _PLATFORM_NAME,
            # Den Channels deliveries are internal Gateway events backed by
            # durable delivery state.  The Hermes gateway must queue them while
            # an agent is busy instead of interrupting or spending the final
            # delivery reply handle on a visible busy ack.  Advertise that
            # operator-visible state lives in the gateway status snapshot.
            "busy_delivery_policy": "force_queue_internal_no_busy_ack",
            "pending_delivery_observability": [
                "gateway_status.active_sessions.queued_events",
                "gateway_status.queued_events",
            ],
            "safe_pending_notifications": "status_only_no_mid_generation_injection",
        }
        binding: dict[str, Any] = {
            "adapter_kind": "hermes_profile",
            "adapter_instance_id": self.adapter_instance_id,
            "agent_identity": self.agent_identity,
            "project_id": self.project_id or None,
            "role": self.role,
            "profile": self.profile,
            "status": "active",
            "capabilities_json": json.dumps(capabilities, sort_keys=True),
        }
        if self.pool_member_id:
            binding["pool_member_id"] = self.pool_member_id
        if self.agent_instance_id:
            binding["agent_instance_id"] = self.agent_instance_id
        return binding

    def _claim_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "adapter_kind": "hermes_profile",
            "adapter_instance_id": self.adapter_instance_id,
            "agent_identity": self.agent_identity,
            "role": self.role,
            "accepted_delivery_modes": ["wake", "notify"],
            "limit": self.claim_limit,
            "lease_seconds": self.lease_seconds,
        }
        if self.project_id:
            payload["project_id"] = self.project_id
        if self.pool_member_id:
            payload["pool_member_id"] = self.pool_member_id
        if self.agent_instance_id:
            payload["agent_instance_id"] = self.agent_instance_id
        return payload

    async def _claim_loop(self) -> None:
        while self._running:
            try:
                claims = await self.gateway_client.claim_deliveries(self._claim_payload())
                for delivery in claims:
                    try:
                        event = await self.delivery_to_event(delivery)
                        await self.handle_message(event)
                    except Exception as exc:
                        delivery_id = _coerce_int(_first(delivery, "delivery_request_id", "deliveryRequestId", "id"))
                        if delivery_id is not None:
                            dummy = _DeliveryContext(
                                delivery_request_id=delivery_id,
                                attempt_id=_coerce_int(_first(delivery, "attempt_id", "attemptId")),
                                project_id=str(_first(delivery, "project_id", "projectId", default=self.project_id) or self.project_id),
                                channel_id=0,
                                trigger_message_id=None,
                                thread_root_message_id=None,
                                session_key="",
                                session_id=None,
                                original_source_kind=str(_first(delivery, "source_kind", "sourceKind", default="") or ""),
                                original_source_id=str(_first(delivery, "source_id", "sourceId", default="") or ""),
                                raw_delivery=dict(delivery or {}),
                            )
                            await self._mark_failed(dummy, "delivery_conversion_failed", exc)
                await self._maybe_sleep(self.claim_interval_seconds if not claims else 0)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("[DenChannels] delivery claim loop failed", exc_info=True)
                await self._maybe_sleep(self.claim_interval_seconds)

    async def _event_poll_loop(self) -> None:
        """Poll Channels for direct-agent wake events targeted at this agent."""
        while self._running:
            try:
                # Periodically discover subscriptions and sync cursors from server
                await self._discover_and_sync_subscriptions()

                channels_to_poll = await self._resolve_poll_channels()
                for channel_id in channels_to_poll:
                    events = await self._event_poller.poll(channel_id, limit=self.poll_limit)
                    if events:
                        logger.info("[DenChannels] polled %d event(s) on channel %d", len(events), channel_id)
                    for raw_event in events:
                        try:
                            delivery = self._event_to_delivery(raw_event)
                            event = await self.delivery_to_event(delivery)
                            await self.handle_message(event)
                        except Exception:
                            logger.warning(
                                "[DenChannels] event %s conversion failed",
                                raw_event.get("id") or raw_event.get("eventId"),
                                exc_info=True,
                            )
                await self._maybe_sleep(self.poll_interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("[DenChannels] event poll loop failed", exc_info=True)
                await self._maybe_sleep(self.poll_interval_seconds)

    async def _discover_and_sync_subscriptions(self) -> None:
        """Discover active server subscriptions for this agent, update cache,
        remove disappeared channels from poller/poll-set, and init cursors.

        Called on connect and periodically during the poll loop (task #2554).
        This is the green path that makes GUI-added memberships produce
        runtime poll subscriptions without profile config edits.

        When subscriptions return empty, clears subscription-derived channels
        from the poll set and removes poller mappings so released/deactivated
        subscriptions stop being polled. Static config channels are never removed.
        """
        import time as _time
        now = _time.time()
        if now - self._last_subscription_discovery < self._subscription_discovery_interval:
            return
        self._last_subscription_discovery = now
        subscription_identity = self.agent_identity

        # Discover active subscriptions from server
        try:
            resp = await self.channels_client.get_subscriptions(
                member_identity=subscription_identity,
                include_inactive=False,
            )
        except Exception:
            logger.debug("[DenChannels] subscription discovery failed", exc_info=True)
            return

        subscriptions: list[dict[str, Any]] = (
            (resp if isinstance(resp, dict) else {})
            .get("subscriptions") or []
        )

        # Build the new subscription-derived channel set
        new_sub_channel_ids: set[int] = set()
        new_sub_mapping: dict[int, int] = {}  # channel_id -> subscription_id

        for sub in subscriptions:
            sub_id = _coerce_int(sub.get("id"))
            ch_id = _coerce_int(sub.get("channelId") or sub.get("channel_id"))
            status = str(sub.get("subscriptionStatus") or sub.get("subscription_status") or "").lower()
            if sub_id is not None and ch_id is not None and status == "active":
                new_sub_channel_ids.add(ch_id)
                new_sub_mapping[ch_id] = sub_id

        # Determine which subscription channels were REMOVED since last discovery
        # _subscription_cache maps channel_id -> subscription_id; keys() are channel IDs
        old_sub_channel_ids: set[int] = set(self._subscription_cache.keys())
        removed_channels = old_sub_channel_ids - new_sub_channel_ids

        # Remove mappings for disappeared subscriptions from the poller
        for ch_id in removed_channels:
            self._event_poller.remove_subscription_mapping(ch_id)

        # Remove disappeared subscription channels from the poll set
        # (static config channels are never removed here — they live in poll_channel_ids)
        self._polled_channels -= removed_channels

        # Update subscription cache
        self._subscription_cache = dict(new_sub_mapping)

        # Add/update poller mappings for new or still-active subscriptions
        for ch_id, sub_id in new_sub_mapping.items():
            self._event_poller.update_subscription_mapping(sub_id, ch_id)

        # Initialize cursors from server for all active subscriptions
        if new_sub_mapping:
            try:
                await self._event_poller.initialize_cursors_from_server()
            except Exception:
                logger.debug("[DenChannels] cursor init from server failed, using local cursors", exc_info=True)

        # Add subscription-derived channels to poll set
        self._polled_channels |= new_sub_channel_ids

        self._subscription_discovery_ever_ran = True

        if new_sub_mapping or removed_channels:
            logger.info(
                "[DenChannels] subscription discovery: %d active, %d removed, channels: %s",
                len(new_sub_mapping), len(removed_channels), sorted(new_sub_channel_ids),
            )

    async def _resolve_poll_channels(self) -> list[int]:
        """Return channel IDs this adapter should poll for direct-agent events."""
        import time as _time
        import httpx

        if not self.channels_url or not self.agent_identity:
            return []
        static_channels = set(self.poll_channel_ids)
        now = _time.time()
        if self._polled_channels and now - self._last_channel_discovery < self._channel_discovery_interval:
            return sorted(self._polled_channels)
        if not self._polled_channels and now - self._last_channel_discovery < self._channel_discovery_interval:
            return []
        self._last_channel_discovery = now

        channels_token = str(getattr(self.channels_client, "token", None) or "").strip() or None
        headers = {"Content-Type": "application/json"}
        if channels_token:
            headers["Authorization"] = f"Bearer {channels_token}"

        discovered_channels: set[int] = set()
        member_discovery_succeeded = False

        async def _check_memberships(query: dict[str, Any]) -> None:
            from urllib.parse import urlencode

            scoped = {
                "memberIdentity": self.agent_identity,
                "includeLeft": "false",
                "includeOrdinaryMemberships": "true",
                "limit": "200",
            }
            scoped.update(query)
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    f"{self.channels_url}/api/channel-memberships?{urlencode(scoped)}",
                    headers=headers,
                )
                if response.status_code != 200:
                    return
                body = response.json() if response.content else {}
                memberships = body.get("memberships") or body.get("items") or []
                for item in memberships:
                    cid = _coerce_int(item.get("channelId") or item.get("channel_id"))
                    status = str(item.get("membershipStatus") or item.get("membership_status") or "active").lower()
                    if cid is not None and status != "left":
                        discovered_channels.add(cid)

        async def _discover_member_channels() -> None:
            response_body: dict[str, Any] = {}
            getter = getattr(self.channels_client, "get_channel_memberships", None)
            if callable(getter):
                try:
                    result = getter(
                        member_identity=self.agent_identity,
                        include_left=False,
                        include_ordinary_memberships=True,
                        limit=200,
                    )
                except TypeError:
                    result = getter(member_identity=self.agent_identity, include_left=False, limit=200)
                maybe_body = await result if inspect.isawaitable(result) else result
                response_body = maybe_body if isinstance(maybe_body, dict) else {}
            else:
                from urllib.parse import urlencode

                query = urlencode({
                    "memberIdentity": self.agent_identity,
                    "includeLeft": "false",
                    "includeOrdinaryMemberships": "true",
                    "limit": "200",
                })
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.get(f"{self.channels_url}/api/channel-memberships?{query}", headers=headers)
                    if response.status_code != 200:
                        return
                    response_body = response.json() if response.content else {}

            memberships = response_body.get("memberships") or response_body.get("items") or []
            for item in memberships:
                cid = _coerce_int(item.get("channelId") or item.get("channel_id"))
                status = str(item.get("membershipStatus") or item.get("membership_status") or "active").lower()
                if cid is not None and status != "left":
                    discovered_channels.add(cid)

        try:
            await _discover_member_channels()
            member_discovery_succeeded = True
        except Exception:
            logger.debug("[DenChannels] member channel discovery failed", exc_info=True)

        if self._subscription_discovery_ever_ran:
            # Subscription discovery is authoritative for runtime poll scope.
            # Membership discovery should NOT add channels back that subscription
            # release/removal removed.  Static config channels are always preserved.
            self._polled_channels |= static_channels
            return sorted(self._polled_channels)

        if discovered_channels or static_channels:
            self._polled_channels = set(discovered_channels) | static_channels
            logger.info(
                "[DenChannels] poll channels resolved from static config/memberships: %s",
                sorted(self._polled_channels),
            )
            return sorted(self._polled_channels)

        # Den system/direct-agent channels are common control channels; project_id can
        # discover a project default channel when configured. Worker profiles should
        # prefer explicit poll_channel_ids/channel_ids so #worker-pool (or another
        # neutral control channel) is not confused with a project waystation.
        if member_discovery_succeeded:
            self._polled_channels = set(static_channels)
            return sorted(self._polled_channels)

        for cid in (672,):
            try:
                await _check_memberships({"channelId": cid})
            except Exception:
                logger.debug("[DenChannels] channel discovery failed for %s", cid, exc_info=True)
        if not discovered_channels and self.project_id:
            try:
                await _check_memberships({"projectId": self.project_id})
            except Exception:
                logger.debug("[DenChannels] project channel discovery failed", exc_info=True)
        self._polled_channels = set(discovered_channels) | static_channels
        if self._polled_channels:
            logger.info("[DenChannels] poll channels discovered: %s", sorted(self._polled_channels))
        return sorted(self._polled_channels)

    @staticmethod
    def _event_to_delivery(raw_event: dict[str, Any]) -> dict[str, Any]:
        """Convert a Channels direct-agent wake_event to delivery_to_event shape."""
        event_id = _coerce_int(raw_event.get("id") or raw_event.get("eventId") or 0) or 0
        channel_id = _coerce_int(raw_event.get("channelId") or raw_event.get("channel_id"))
        metadata: dict[str, Any] = {}
        metadata_str = raw_event.get("metadataJson") or raw_event.get("metadata_json") or raw_event.get("metadata")
        if isinstance(metadata_str, str) and metadata_str.strip():
            try:
                metadata = json.loads(metadata_str)
            except Exception:
                metadata = {}
        elif isinstance(metadata_str, dict):
            metadata = metadata_str
        if channel_id is not None and "channel_id" not in metadata and "channelId" not in metadata:
            metadata["channel_id"] = channel_id
        human_body = raw_event.get("body") or raw_event.get("text") or raw_event.get("content") or ""
        summary = raw_event.get("summary") or ""
        delivery: dict[str, Any] = {
            "delivery_request_id": event_id,
            "project_id": str(raw_event.get("sourceProjectId") or raw_event.get("source_project_id") or raw_event.get("projectId") or ""),
            "source_kind": str(raw_event.get("sourceKind") or raw_event.get("source_kind") or "wake_event"),
            "source_id": str(raw_event.get("sourceId") or raw_event.get("source_id") or ""),
            "body": human_body,
            "context_summary": summary or human_body,
            "metadata_json": json.dumps(metadata, sort_keys=True, default=str) if metadata else "{}",
            "channel_id": channel_id,
            "target_project_id": raw_event.get("targetProjectId") or raw_event.get("target_project_id"),
            "target_task_id": raw_event.get("targetTaskId") or raw_event.get("target_task_id"),
            "assignment_id": raw_event.get("assignmentId") or raw_event.get("assignment_id"),
            "worker_run_id": raw_event.get("workerRunId") or raw_event.get("worker_run_id"),
            "worker_role": raw_event.get("workerRole") or raw_event.get("worker_role"),
            "profile_identity": raw_event.get("profileIdentity") or raw_event.get("profile_identity"),
            "pool_member_id": raw_event.get("poolMemberId") or raw_event.get("pool_member_id"),
            "agent_instance_id": raw_event.get("agentInstanceId") or raw_event.get("agent_instance_id"),
            "session_owner_id": raw_event.get("sessionOwnerId") or raw_event.get("session_owner_id"),
            "session_id": raw_event.get("sessionId") or raw_event.get("session_id"),
        }
        for meta_key in ("deliveryStatus", "claimStatus", "completionStatus", "wakePolicy"):
            meta_val = metadata.get(meta_key)
            if meta_val is not None:
                delivery[meta_key] = meta_val
        return delivery


    async def _maybe_sleep(self, seconds: float) -> None:
        result = self._sleep(seconds)
        if inspect.isawaitable(result):
            await result

    def _context_for_event(self, event: MessageEvent) -> _DeliveryContext | None:
        task = asyncio.current_task()
        if task is not None and task in self._contexts_by_task:
            return self._contexts_by_task[task]
        if event is None or event.source is None:
            return None
        session_key = build_session_key(event.source, group_sessions_per_user=False)
        return self._contexts_by_session.get(session_key) or self._contexts_by_chat.get(str(event.source.chat_id))

    def _context_for_send(self, chat_id: str, metadata: dict[str, Any] | None) -> _DeliveryContext | None:
        task = asyncio.current_task()
        task_context = self._contexts_by_task.get(task) if task is not None else None
        if metadata:
            delivery_id = _coerce_int(_first(
                metadata,
                "delivery_request_id",
                "deliveryRequestId",
                "den_channels_delivery_request_id",
                "denChannelsDeliveryRequestId",
            ))
            if delivery_id is not None:
                explicit_context = self._contexts_by_delivery_id.get(delivery_id)
                if explicit_context is not None:
                    return explicit_context
                if task_context is not None and task_context.delivery_request_id == delivery_id:
                    return task_context
                session_key = metadata.get("session_key") or metadata.get("sessionKey")
                session_context = self._contexts_by_session.get(session_key) if session_key else None
                if session_context is not None and session_context.delivery_request_id == delivery_id:
                    return session_context
                thread_id = metadata.get("thread_id") or metadata.get("threadId")
                thread_context = None
                if thread_id is not None:
                    thread_context = self._contexts_by_lane.get(self._lane_key(chat_id, str(thread_id)))
                if thread_context is not None and thread_context.delivery_request_id == delivery_id:
                    return thread_context
                lane_context = self._contexts_by_lane.get(self._lane_key(chat_id, None)) or self._contexts_by_chat.get(str(chat_id))
                if lane_context is not None and lane_context.delivery_request_id == delivery_id:
                    return lane_context
                return None
        if task_context is not None:
            return task_context
        if metadata:
            session_key = metadata.get("session_key") or metadata.get("sessionKey")
            if session_key and session_key in self._contexts_by_session:
                return self._contexts_by_session[session_key]
            thread_id = metadata.get("thread_id") or metadata.get("threadId")
            if thread_id is not None:
                context = self._contexts_by_lane.get(self._lane_key(chat_id, str(thread_id)))
                if context is not None:
                    return context
        return self._contexts_by_lane.get(self._lane_key(chat_id, None)) or self._contexts_by_chat.get(str(chat_id))

    def _clear_context(self, context: _DeliveryContext) -> None:
        """Remove only mappings that still point at this delivery context."""
        active_activity_context = _activity_context()
        if active_activity_context:
            if str(active_activity_context.get("deliveryRequestId") or "") == str(context.delivery_request_id):
                self._clear_activity_environment(context)
        chat_id = f"project:{context.project_id}:channel:{context.channel_id}"
        if self._contexts_by_session.get(context.session_key) is context:
            self._contexts_by_session.pop(context.session_key, None)
        # Clear raw channel id mapping (used by non-lane contexts)
        if self._contexts_by_chat.get(chat_id) is context:
            self._contexts_by_chat.pop(chat_id, None)
        if context.session_owner_id is not None and self._contexts_by_chat.get(context.session_owner_id) is context:
            self._contexts_by_chat.pop(context.session_owner_id, None)
        # Clear explicit lane mapping (when conversation_lane_id was used as chat_id)
        if context.conversation_lane_id is not None:
            lane_chat_id = context.conversation_lane_id
            if self._contexts_by_chat.get(lane_chat_id) is context:
                self._contexts_by_chat.pop(lane_chat_id, None)
            lane_channel_key = self._lane_key(lane_chat_id, None)
            if self._contexts_by_lane.get(lane_channel_key) is context:
                self._contexts_by_lane.pop(lane_channel_key, None)
            lane_thread_key = self._lane_key(
                lane_chat_id,
                f"thread:{context.thread_root_message_id}"
                if context.thread_root_message_id is not None
                else None,
            )
            if self._contexts_by_lane.get(lane_thread_key) is context:
                self._contexts_by_lane.pop(lane_thread_key, None)
        channel_key = self._lane_key(chat_id, None)
        if self._contexts_by_lane.get(channel_key) is context:
            self._contexts_by_lane.pop(channel_key, None)
        thread_key = self._lane_key(
            chat_id,
            f"thread:{context.thread_root_message_id}"
            if context.thread_root_message_id is not None
            else None,
        )
        if self._contexts_by_lane.get(thread_key) is context:
            self._contexts_by_lane.pop(thread_key, None)
        if self._contexts_by_delivery_id.get(context.delivery_request_id) is context:
            self._contexts_by_delivery_id.pop(context.delivery_request_id, None)
        for task, task_context in list(self._contexts_by_task.items()):
            if task_context is context:
                self._contexts_by_task.pop(task, None)

    @staticmethod
    def _lane_key(chat_id: Any, thread_id: Any = None) -> tuple[str, str | None]:
        return (str(chat_id), str(thread_id) if thread_id not in {None, ""} else None)

    async def _mark_failed(self, context: _DeliveryContext, category: str, diagnostic: Any) -> None:
        if context.delivery_request_id in self._terminal_delivery_ids:
            return
        redacted_diagnostic = _redact(diagnostic)
        diagnostic_json = json.dumps(redacted_diagnostic, sort_keys=True, default=str)[:4000]
        payload = {
            "attempt_id": context.attempt_id,
            "adapter_kind": "hermes_profile",
            "adapter_instance_id": self.adapter_instance_id,
            "session_id": context.session_id,
            "error_code": category,
            "error_message": str(redacted_diagnostic)[:1000],
            # Keep legacy keys in payloads for in-process diagnostics; Den Gateway
            # ignores unknown JSON properties and persists the canonical fields.
            "failure_category": category,
            "diagnostic_json": diagnostic_json,
            "metadata_json": json.dumps(
                {
                    "delivery_request_id": context.delivery_request_id,
                    "failure_category": category,
                    "diagnostic": redacted_diagnostic,
                },
                sort_keys=True,
                default=str,
            )[:4000],
        }
        if self.gateway_client is None:
            logger.debug(
                "[DenChannels] no Gateway delivery client configured; skipping delivery %s failed marker",
                context.delivery_request_id,
            )
            self._terminal_delivery_ids.add(context.delivery_request_id)
            return
        try:
            await self.gateway_client.mark_failed(context.delivery_request_id, payload)
            self._terminal_delivery_ids.add(context.delivery_request_id)
        except Exception:
            logger.debug("[DenChannels] failed to mark delivery %s failed", context.delivery_request_id, exc_info=True)


def check_requirements() -> bool:
    try:
        import httpx  # noqa: F401
        return True
    except Exception:
        return False


def validate_config(config: PlatformConfig) -> bool:
    extra = _extra(config)
    gateway_url = extra.get("gateway_url") or os.getenv("DEN_GATEWAY_URL")
    channels_url = extra.get("channels_url") or os.getenv("DEN_CHANNELS_URL")
    agent_identity = extra.get("agent_identity") or os.getenv("HERMES_AGENT_IDENTITY") or os.getenv("HERMES_PROFILE")
    token = str(extra.get("token") or os.getenv("DEN_GATEWAY_TOKEN") or "").strip()
    return bool(
        channels_url
        and agent_identity
        and (token or _is_private_url(str(gateway_url or "")) or _is_private_url(str(channels_url)))
    )


_DIRECT_AGENT_MESSAGE_PARAMETERS = {
    "type": "object",
    "properties": {
        "channel_id": {
            "type": "integer",
            "description": "Den Channels ID for the target channel. Required unless project_id is provided.",
        },
        "project_id": {
            "type": "string",
            "description": "Project slug to resolve the default channel. Required unless channel_id is provided.",
        },
        "member_identity": {
            "type": "string",
            "description": (
                "Logical active Channels member identity to wake, e.g. spawned-reviewer. "
                "Do not pass a concrete pool member id here unless that exact id is an active membership."
            ),
        },
        "body": {
            "type": "string",
            "description": "Message body to deliver.",
        },
        "sender_identity": {
            "type": "string",
            "description": "Sending agent identity. Defaults to the active agent identity.",
        },
        "source_project_id": {
            "type": "string",
            "description": "Optional source project for work/wake attribution.",
        },
        "target_task_id": {
            "type": "integer",
            "description": "Optional Den task id for the target work item.",
        },
        "assignment_id": {
            "type": "string",
            "description": "Optional concrete Den worker assignment id for wake correlation.",
        },
        "worker_run_id": {
            "type": "string",
            "description": "Optional Den worker run id for wake correlation.",
        },
        "worker_role": {
            "type": "string",
            "description": "Optional worker role, e.g. coder or reviewer.",
        },
        "profile_identity": {
            "type": "string",
            "description": "Optional logical worker profile identity, e.g. spawned-reviewer.",
        },
        "pool_member_id": {
            "type": "string",
            "description": "Optional concrete pool member selector, e.g. pool-reviewer-03.",
        },
        "agent_instance_id": {
            "type": "string",
            "description": "Optional concrete agent instance selector for the selected pool member.",
        },
    },
    "required": ["member_identity", "body"],
}

_DIRECT_AGENT_MESSAGE_SCHEMA = {
    "description": "Send a direct agent message via Den Channels Gateway. Requires member_identity — broadcast is not supported.",
    "parameters": _DIRECT_AGENT_MESSAGE_PARAMETERS,
}


def _check_direct_agent_message_available() -> bool:
    """Return True if direct-agent message base URL is available."""
    return bool(
        os.getenv("DEN_DELIVERY_URL")
        or os.getenv("DEN_GATEWAY_URL")
        or _DIRECT_AGENT_CONFIG_DEFAULTS.get("delivery_url")
        or _DIRECT_AGENT_CONFIG_DEFAULTS.get("gateway_url")
    )


def _classify_direct_agent_failure(exc: Exception) -> str:
    """Classify a direct-agent message failure for structured diagnostics.

    Distinguishes route/client errors (404, connection refused, DNS) from
    capacity errors so failure packets can route to the correct remediation
    path.
    """
    import httpx

    msg = str(exc).lower()
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 404:
            return "worker_wake_channels_route_404"
        if status in (401, 403):
            return "worker_wake_channels_auth_error"
        return f"worker_wake_channels_http_{status}"
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        if "name or service not known" in msg or "nodename nor servname" in msg:
            return "worker_wake_channels_dns_error"
        return "worker_wake_channels_connection_error"
    if "timeout" in msg:
        return "worker_wake_channels_timeout"
    if "name or service not known" in msg or "nodename nor servname" in msg or "dns" in msg or "resolve" in msg:
        return "worker_wake_channels_dns_error"
    if "connection refused" in msg:
        return "worker_wake_channels_connection_error"
    return "worker_wake_channels_client_error"



def _build_target_identity(
    member_identity: str,
    pool_member_id: str | None = None,
    agent_instance_id: str | None = None,
) -> dict[str, str]:
    """Build a Delivery target_identity from wake parameters.

    ``profile`` is always the ``member_identity``.  ``instance_id`` prefers
    the concrete worker selector (agent_instance_id or pool_member_id), then
    falls back to ``{member_identity}@unknown``.
    """
    instance = agent_instance_id or pool_member_id or f"{member_identity}@unknown"
    return {"profile": member_identity, "instance_id": str(instance).strip()}


def _build_wake_idempotency_key(
    member_identity: str,
    channel_id: int | None = None,
    project_id: str | None = None,
    worker_run_id: str | None = None,
) -> str:
    """Build a deterministic idempotency key for a wake delivery intent.

    Format: ``wake:{channel_or_project}:{profile}:{nonce}`` — matches the
    delivery service's ``idempotency.Parse()`` contract.
    """
    channel_or_project = (
        f"ch{channel_id}" if channel_id is not None
        else f"pj{project_id}" if project_id
        else "global"
    )
    nonce = worker_run_id or str(int(time.time()))
    return f"wake:{channel_or_project}:{member_identity}:{nonce}"


async def _handle_direct_agent_message(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Handler for the den_channels_send_direct_agent_message tool.

    Creates a Delivery intent via ``POST /v1/delivery/intents`` to wake
    a target runtime.  The wake body/context is carried in ``source_ref``
    for traceability; durable human-facing evidence should go through the
    Conversation successor before creating the intent (passing
    ``channel_message_id``).

    Task #3031 — successor delivery service migration.
    """
    if isinstance(args, dict):
        merged_args = dict(args)
        merged_args.update(kwargs)
    else:
        merged_args = dict(kwargs)

    channel_id = merged_args.get("channel_id")
    project_id = merged_args.get("project_id")
    member_identity = merged_args.get("member_identity")
    body = merged_args.get("body")
    sender_identity = merged_args.get("sender_identity")
    activity_context = _activity_context()
    source_project_id = merged_args.get("source_project_id") or merged_args.get("sourceProjectId")
    target_task_id = merged_args.get("target_task_id") or merged_args.get("targetTaskId")
    assignment_id = merged_args.get("assignment_id") or merged_args.get("assignmentId")
    worker_run_id = (
        merged_args.get("worker_run_id")
        or merged_args.get("workerRunId")
        or activity_context.get("workerRunId")
        or activity_context.get("worker_run_id")
    )
    worker_role = (
        merged_args.get("worker_role")
        or merged_args.get("workerRole")
        or activity_context.get("workerRole")
        or activity_context.get("worker_role")
    )
    pool_member_id = (
        merged_args.get("pool_member_id")
        or merged_args.get("poolMemberId")
        or activity_context.get("poolMemberId")
        or activity_context.get("pool_member_id")
    )
    profile_identity = (
        merged_args.get("profile_identity")
        or merged_args.get("profileIdentity")
        or activity_context.get("profileIdentity")
        or activity_context.get("profile_identity")
    )
    agent_instance_id = (
        merged_args.get("agent_instance_id")
        or merged_args.get("agentInstanceId")
        or activity_context.get("agentInstanceId")
        or activity_context.get("agent_instance_id")
    )

    if not member_identity:
        return json.dumps({"status": "error", "error": "member_identity is required"})
    if not body:
        return json.dumps({"status": "error", "error": "body is required"})
    if not channel_id and not project_id:
        return json.dumps({"status": "error", "error": "channel_id or project_id is required"})

    delivery_url = (
        os.getenv("DEN_DELIVERY_URL")
        or _DIRECT_AGENT_CONFIG_DEFAULTS.get("delivery_url")
        or ""
    ).rstrip("/")
    gateway_url = (
        os.getenv("DEN_GATEWAY_URL")
        or _DIRECT_AGENT_CONFIG_DEFAULTS.get("gateway_url")
        or str(activity_context.get("gatewayUrl") or "")
        or ""
    ).rstrip("/")
    # Use delivery_url as the primary base for this delivery intent.
    # gateway_url is the fallback; channels_url is intentionally NOT used
    # because this is an executable wake, not conversation.
    base_url = delivery_url or gateway_url
    if not base_url:
        return json.dumps({"status": "error", "error": "DEN_DELIVERY_URL or DEN_GATEWAY_URL is not configured"})

    target_identity = _build_target_identity(member_identity, pool_member_id, agent_instance_id)
    idempotency_key = _build_wake_idempotency_key(member_identity, channel_id, project_id, worker_run_id)

    # Carry the wake body/context in source_ref for traceability.
    # Format: wake://{profile}?body={encoded_context}
    source_ref = f"wake://{member_identity}?body={body[:2000]}" if body else None

    payload: dict[str, Any] = {
        "target_identity": target_identity,
        "idempotency_key": idempotency_key,
        "source_ref": source_ref,
        "ttl_seconds": 300,
    }

    effective_sender = (
        str(sender_identity or "").strip()
        or str(activity_context.get("agentIdentity") or "").strip()
        or _DIRECT_AGENT_CONFIG_DEFAULTS.get("agent_identity")
        or os.getenv("HERMES_AGENT_IDENTITY")
        or os.getenv("HERMES_PROFILE")
        or "hermes"
    )

    # Include work-attribution metadata when present for correlation (#1911, #3031).
    if channel_id is not None:
        payload["channel_id"] = int(channel_id) if not isinstance(channel_id, int) else channel_id
    if project_id:
        payload["project_id"] = str(project_id).strip()
    if source_project_id:
        payload["source_project_id"] = str(source_project_id).strip()
    if target_task_id is not None:
        payload["target_task_id"] = int(target_task_id) if not isinstance(target_task_id, int) else target_task_id
    if assignment_id is not None:
        payload["assignment_id"] = str(assignment_id).strip()
    if worker_run_id:
        payload["worker_run_id"] = str(worker_run_id)
    if worker_role:
        payload["worker_role"] = str(worker_role)
    if pool_member_id:
        payload["pool_member_id"] = str(pool_member_id)
    if profile_identity:
        payload["profile_identity"] = str(profile_identity)
    if agent_instance_id:
        payload["agent_instance_id"] = str(agent_instance_id)

    endpoint_path = "/v1/delivery/intents"
    endpoint = join_api_url(base_url, endpoint_path)
    headers = {"Content-Type": "application/json"}
    token = str(
        os.getenv("DEN_GATEWAY_TOKEN")
        or os.getenv("DEN_CHANNELS_TOKEN")
        or _DIRECT_AGENT_CONFIG_DEFAULTS.get("token")
        or ""
    ).strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        import httpx

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                endpoint,
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json() if response.content else {}
    except Exception as exc:
        safe_exc = _redact(str(exc))
        diagnostic = {
            "status": "error",
            "error": safe_exc,
            "base_url": base_url,
            "endpoint": endpoint,
            "endpoint_path": endpoint_path,
            "request_shape": _redact(payload),
            "failure_category": _classify_direct_agent_failure(exc),
        }
        return json.dumps(diagnostic, sort_keys=True, default=str)

    return json.dumps({
        "status": "ok",
        "delivery_intent_id": data.get("id") if isinstance(data, dict) else None,
        "message": data,
    })


def register(ctx: Any) -> None:
    """Plugin entry point: called by the Hermes plugin system."""
    # Suppress the generic home-channel onboarding notice for Den Channels.
    # Den Channels delivery lanes are already the delivery context; /sethome
    # has no useful meaning inside a project lane.
    import os as _os
    _os.environ.setdefault("DEN_CHANNELS_HOME_CHANNEL", "suppressed")
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_platform(
        name=_PLATFORM_NAME,
        label="Den Channels",
        adapter_factory=lambda cfg: DenChannelsAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=["DEN_GATEWAY_URL", "DEN_CHANNELS_URL"],
        allowed_users_env="DEN_CHANNELS_ALLOWED_USERS",
        allow_all_env="DEN_CHANNELS_ALLOW_ALL_USERS",
        install_hint="No extra packages needed beyond Hermes runtime dependencies",
        max_message_length=0,
        pii_safe=True,
        emoji="🕸️",
        allow_update_command=False,
        platform_hint=(
            "You are chatting through Den Channels. Keep Den task/review records as "
            "the durable source of truth and use lane-scoped /new semantics."
        ),
    )
    ctx.register_tool(
        name="den_channels_send_direct_agent_message",
        toolset="den_channels",
        schema=_DIRECT_AGENT_MESSAGE_SCHEMA,
        handler=_handle_direct_agent_message,
        check_fn=_check_direct_agent_message_available,
        is_async=True,
        description=(
            "Send a direct agent message through Den Channels Gateway to a specific "
            "agent member. Requires member_identity as the target — broadcast is not "
            "supported. Uses DEN_CHANNELS_URL / DEN_GATEWAY_URL from environment or "
            "plugin config."
        ),
        emoji="📨",
    )

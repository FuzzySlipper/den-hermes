"""Native Hermes Gateway adapter for Den Channels durable sessions.

This adapter treats Den Channels as a first-class Hermes Gateway platform.  It
claims delivery requests from Den Gateway, turns them into normal GatewayRunner
``MessageEvent`` objects with stable Den Channels session lanes, and posts final
assistant replies back to Den Channels as ``gateway_delivery`` messages.
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
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit

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
    gateway_url = str(context.get("gatewayUrl") or context.get("gateway_url") or "").rstrip("/")
    channel_id = context.get("channelId") or context.get("channel_id")
    if not gateway_url or not channel_id:
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
            response = client.post(f"{gateway_url}/api/channel-activity-events", json=request_payload, headers=headers)
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


_DIRECT_AGENT_CONFIG_DEFAULTS: dict[str, str] = {}


def _remember_direct_agent_config(
    *,
    gateway_url: str = "",
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
        "channels_url": (channels_url or "").rstrip("/"),
        "agent_identity": (agent_identity or "").strip(),
    }
    if token:
        updates["token"] = token
    _DIRECT_AGENT_CONFIG_DEFAULTS.update({key: value for key, value in updates.items() if value})


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

    async def get_gateway_message(self, message_id: str | int) -> dict[str, Any]:
        return await self._request("GET", f"/api/gateway/messages/{message_id}")

    async def post_channel_message(self, channel_id: str | int, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", f"/api/channels/{channel_id}/messages", payload)

    async def add_reaction(self, message_id: str | int, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", f"/api/channel-messages/{message_id}/reactions", payload)


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
        self.project_id = str(extra.get("project_id") or os.getenv("DEN_CHANNELS_PROJECT_ID") or "").strip()
        self.agent_identity = str(extra.get("agent_identity") or os.getenv("HERMES_AGENT_IDENTITY") or os.getenv("HERMES_PROFILE") or "hermes").strip()
        self.role = str(extra.get("role") or os.getenv("HERMES_AGENT_ROLE") or "agent").strip()
        self.profile = str(extra.get("profile") or os.getenv("HERMES_PROFILE") or self.agent_identity).strip()
        self.adapter_instance_id = str(
            extra.get("adapter_instance_id")
            or os.getenv("DEN_CHANNELS_ADAPTER_INSTANCE_ID")
            or f"{socket.gethostname()}:{self.profile}:{self.role}:gateway"
        )
        self.claim_interval_seconds = float(extra.get("claim_interval_seconds") or 2.0)
        self.claim_limit = max(1, _coerce_int(extra.get("claim_limit")) or 1)
        self.lease_seconds = max(1, _coerce_int(extra.get("lease_seconds")) or 300)
        self.start_claim_loop = _coerce_bool(extra.get("start_claim_loop"), True)
        self._sleep = sleep or asyncio.sleep
        token = str(extra.get("token") or os.getenv("DEN_GATEWAY_TOKEN") or "").strip() or None
        channels_token = str(extra.get("channels_token") or os.getenv("DEN_CHANNELS_TOKEN") or token or "").strip() or None
        _remember_direct_agent_config(
            gateway_url=self.gateway_url,
            channels_url=self.channels_url,
            token=channels_token or token,
            agent_identity=self.agent_identity,
        )
        self._has_trusted_transport = bool(token or _is_private_url(self.gateway_url))
        self.gateway_client = gateway_client or DenGatewayClient(self.gateway_url, token=token)
        self.channels_client = channels_client or DenChannelsClient(self.channels_url, token=channels_token)
        self._claim_task: asyncio.Task | None = None
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
        await self.gateway_client.upsert_adapter_binding(self._binding_payload())
        self._running = True
        if self.start_claim_loop:
            try:
                self._claim_task = asyncio.create_task(self._claim_loop())
            except RuntimeError:
                # Unit tests may construct without a running loop; connect still proves config/binding.
                self._claim_task = None
        return True

    async def disconnect(self) -> None:
        self._running = False
        if self._claim_task and not self._claim_task.done():
            self._claim_task.cancel()
            try:
                await self._claim_task
            except asyncio.CancelledError:
                pass
        self._claim_task = None

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
            message = await self.channels_client.get_gateway_message(source_id)
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
        body = str(_first(message, "body", "text", "content", default=_first(delivery, "context_summary", "contextSummary", default="")) or "")
        chat_name = _first(metadata, "channel_slug", "channelSlug", "channel_name", "channelName")
        chat_type = "thread" if thread_root is not None else "channel"
        chat_id = f"project:{project_id}:channel:{channel_id}"
        thread_id = f"thread:{thread_root}" if thread_root is not None else None
        source = self.build_source(
            chat_id=chat_id,
            chat_name=str(chat_name) if chat_name else None,
            chat_type=chat_type,
            user_id=sender,
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
        )

    async def on_processing_start(self, event: MessageEvent) -> None:
        self.set_delivery_context(event)
        context = self._context_for_event(event)
        if context is not None:
            self._set_activity_environment(context)

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
                    self._clear_context(context)
                return
            context = self._build_context(event) or context or self._context_for_event(event)
            if context is not None:
                await self._mark_failed(context, f"processing_{getattr(outcome, 'value', outcome)}", event.raw_message)
                self._clear_context(context)
        finally:
            self._clear_activity_environment(context)

    def _set_activity_environment(self, context: _DeliveryContext) -> None:
        payload = {
            "gatewayUrl": self.gateway_url,
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
        return {
            "adapter_kind": "hermes_profile",
            "adapter_instance_id": self.adapter_instance_id,
            "agent_identity": self.agent_identity,
            "project_id": self.project_id or None,
            "role": self.role,
            "profile": self.profile,
            "status": "active",
            "capabilities_json": json.dumps(capabilities, sort_keys=True),
        }

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
        if self._contexts_by_chat.get(chat_id) is context:
            self._contexts_by_chat.pop(chat_id, None)
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
    return bool(gateway_url and channels_url and agent_identity and (token or _is_private_url(str(gateway_url))))


_DIRECT_AGENT_MESSAGE_SCHEMA = {
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
            "description": "Target agent identity. Required — no broadcast. Must be an active Channels member.",
        },
        "body": {
            "type": "string",
            "description": "Message body to deliver.",
        },
        "sender_identity": {
            "type": "string",
            "description": "Sending agent identity. Defaults to the active agent identity.",
        },
    },
    "required": ["member_identity", "body"],
    "description": "Send a direct agent message via Den Channels Gateway. Requires member_identity — broadcast is not supported.",
}


def _check_direct_agent_message_available() -> bool:
    """Return True if direct-agent message base URL is available."""
    return bool(
        os.getenv("DEN_CHANNELS_URL")
        or os.getenv("DEN_GATEWAY_URL")
        or _DIRECT_AGENT_CONFIG_DEFAULTS.get("channels_url")
        or _DIRECT_AGENT_CONFIG_DEFAULTS.get("gateway_url")
    )


async def _handle_direct_agent_message(**kwargs: Any) -> str:
    """Handler for the den_channels_send_direct_agent_message tool.

    Posts a direct-agent message to the Den Gateway
    ``/api/gateway/direct-agent-messages`` endpoint.
    """
    channel_id = kwargs.get("channel_id")
    project_id = kwargs.get("project_id")
    member_identity = kwargs.get("member_identity")
    body = kwargs.get("body")
    sender_identity = kwargs.get("sender_identity")

    if not member_identity:
        return json.dumps({"status": "error", "error": "member_identity is required"})
    if not body:
        return json.dumps({"status": "error", "error": "body is required"})
    if not channel_id and not project_id:
        return json.dumps({"status": "error", "error": "channel_id or project_id is required"})

    activity_context = _activity_context()
    channels_url = (
        os.getenv("DEN_CHANNELS_URL")
        or _DIRECT_AGENT_CONFIG_DEFAULTS.get("channels_url")
        or ""
    ).rstrip("/")
    gateway_url = (
        os.getenv("DEN_GATEWAY_URL")
        or _DIRECT_AGENT_CONFIG_DEFAULTS.get("gateway_url")
        or str(activity_context.get("gatewayUrl") or "")
        or ""
    ).rstrip("/")
    base_url = channels_url or gateway_url
    if not base_url:
        return json.dumps({"status": "error", "error": "DEN_CHANNELS_URL or DEN_GATEWAY_URL is not configured"})

    effective_sender = (
        str(sender_identity or "").strip()
        or str(activity_context.get("agentIdentity") or "").strip()
        or _DIRECT_AGENT_CONFIG_DEFAULTS.get("agent_identity")
        or os.getenv("HERMES_AGENT_IDENTITY")
        or os.getenv("HERMES_PROFILE")
        or "hermes"
    )

    payload: dict[str, Any] = {
        "memberIdentity": str(member_identity).strip(),
        "senderIdentity": effective_sender,
        "body": str(body).strip(),
    }
    if channel_id is not None:
        payload["channelId"] = int(channel_id) if not isinstance(channel_id, int) else channel_id
    if project_id:
        payload["projectId"] = str(project_id).strip()

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
                f"{base_url}/api/gateway/direct-agent-messages",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json() if response.content else {}
    except Exception as exc:
        return json.dumps({
            "status": "error",
            "error": str(exc),
        })

    return json.dumps({
        "status": "ok",
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

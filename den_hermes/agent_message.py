from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlencode

AGENT_COMMONS_CHANNEL_ID = 21
AGENT_COMMONS_SLUG = "agent-commons"
DEFAULT_CHANNELS_BASE_URL = "http://192.168.1.10:18080"


@dataclass(frozen=True)
class AgentMessageResult:
    """Durable evidence for a Den Channels direct-agent message/wake attempt."""

    status: str
    member_identity: str
    channel_id: int | None = None
    channel_slug: str | None = None
    channel_kind: str | None = None
    project_id: str | None = None
    target_project_id: str | None = None
    target_task_id: int | None = None
    target_assignment_id: int | None = None
    message_id: int | str | None = None
    delivery_request_id: int | str | None = None
    gateway_message_url: str | None = None
    gateway_events_url: str | None = None
    delivery_status: str | None = None
    diagnostic: str | None = None


class DenChannelsAgentMessenger:
    """Green-path wrapper for agent-to-agent message/wake through Den Channels.

    Resolution order is intentionally narrow and deterministic:

    1. explicit ``channel_id``;
    2. project default channel via ``project_id``;
    3. Agent Commons (channel id 21) fallback.

    The wrapper preflights active agent membership before sending and returns a
    ``not_sent`` result instead of claiming a wake when the target is not an
    active Channels member.
    """

    def __init__(self, *, tools: Any, base_url: str = DEFAULT_CHANNELS_BASE_URL) -> None:
        self.tools = tools
        self.base_url = base_url.rstrip("/")

    def send_agent_message(
        self,
        *,
        member_identity: str,
        body: str,
        project_id: str | None = None,
        channel_id: int | None = None,
        sender_identity: str | None = None,
        target_project_id: str | None = None,
        target_task_id: int | None = None,
        target_assignment_id: int | None = None,
    ) -> AgentMessageResult:
        memberships = self._resolve_memberships(project_id=project_id, channel_id=channel_id)
        resolved_channel_id = _optional_int(_first_present(memberships, "channelId", "channel_id"))
        channel_slug = _optional_str(_first_present(memberships, "channelSlug", "channel_slug", "slug"))
        channel_kind = _optional_str(_first_present(memberships, "channelKind", "channel_kind", "kind"))
        resolved_project_id = _optional_str(_first_present(memberships, "projectId", "project_id"))
        if resolved_channel_id is None:
            return AgentMessageResult(
                status="not_sent",
                member_identity=member_identity,
                project_id=resolved_project_id or project_id,
                diagnostic="resolved channel membership payload did not include a channel id",
            )

        member = _find_active_agent_member(memberships, member_identity)
        if member is None:
            label = f"{resolved_channel_id} ({channel_slug})" if channel_slug else str(resolved_channel_id)
            return AgentMessageResult(
                status="not_sent",
                member_identity=member_identity,
                channel_id=resolved_channel_id,
                channel_slug=channel_slug,
                channel_kind=channel_kind,
                project_id=resolved_project_id,
                diagnostic=f"{member_identity} is not an active agent member of channel {label}",
            )

        send_args: dict[str, Any] = {
            "channel_id": resolved_channel_id,
            "member_identity": member_identity,
            "body": body,
        }
        if sender_identity:
            send_args["sender_identity"] = sender_identity
        if target_project_id:
            send_args["source_project_id"] = target_project_id
        if target_task_id is not None:
            send_args["target_task_id"] = target_task_id
        if target_assignment_id is not None:
            send_args["assignment_id"] = str(target_assignment_id)
        response = self.tools.den_channels_send_direct_agent_message(**send_args)
        message_id = _extract_message_id(response)
        delivery_request_id = _extract_delivery_request_id(response)
        message_payload = self._read_message(message_id)
        if delivery_request_id is None:
            delivery_request_id = _extract_delivery_request_id(message_payload)
        events_payload = self._read_events(resolved_channel_id, after_id=_events_after_id(message_id))
        delivery_status = _extract_delivery_status(
            events_payload,
            message_id=message_id,
            delivery_request_id=delivery_request_id,
            member_identity=member_identity,
        )
        return AgentMessageResult(
            status="sent",
            member_identity=member_identity,
            channel_id=resolved_channel_id,
            channel_slug=channel_slug,
            channel_kind=channel_kind,
            project_id=resolved_project_id,
            target_project_id=target_project_id,
            target_task_id=target_task_id,
            target_assignment_id=target_assignment_id,
            message_id=message_id,
            delivery_request_id=delivery_request_id,
            gateway_message_url=self._message_url(message_id) if message_id is not None else None,
            gateway_events_url=self._events_url(resolved_channel_id),
            delivery_status=delivery_status,
        )

    def _resolve_memberships(self, *, project_id: str | None, channel_id: int | None) -> Mapping[str, Any]:
        if channel_id is not None:
            response = self.tools.den_channels_get_memberships(channel_id=channel_id)
        elif project_id:
            response = self.tools.den_channels_get_memberships(project_id=project_id)
        else:
            response = self.tools.den_channels_get_memberships(channel_id=AGENT_COMMONS_CHANNEL_ID)
        return _extract_memberships(response)

    def _read_message(self, message_id: int | str | None) -> Mapping[str, Any] | None:
        if message_id is None or not hasattr(self.tools, "den_channels_get_message"):
            return None
        try:
            return self.tools.den_channels_get_message(message_id=message_id)
        except Exception:  # noqa: BLE001 - evidence enrichment is best-effort after send.
            return None

    def _read_events(self, channel_id: int, *, after_id: int = 0) -> Mapping[str, Any] | None:
        if not hasattr(self.tools, "den_channels_get_events"):
            return None
        try:
            return self.tools.den_channels_get_events(channel_id=channel_id, after_id=after_id, limit=50)
        except Exception:  # noqa: BLE001 - evidence enrichment is best-effort after send.
            return None

    def _message_url(self, message_id: int | str) -> str:
        return f"{self.base_url}/api/gateway/messages/{message_id}"

    def _events_url(self, channel_id: int) -> str:
        return f"{self.base_url}/api/gateway/events?{urlencode({'channelId': channel_id, 'afterId': 0})}"


def _extract_memberships(response: Any) -> Mapping[str, Any]:
    if isinstance(response, Mapping):
        value = response.get("memberships") or response.get("result") or response
        if isinstance(value, Mapping):
            return value
    raise ValueError("membership response did not contain a memberships object")


def _find_active_agent_member(memberships: Mapping[str, Any], member_identity: str) -> Mapping[str, Any] | None:
    raw_members = memberships.get("members") or memberships.get("items") or []
    if not isinstance(raw_members, list):
        return None
    for member in raw_members:
        if not isinstance(member, Mapping):
            continue
        identity = _optional_str(_first_present(member, "memberIdentity", "member_identity", "identity"))
        member_type = (_optional_str(_first_present(member, "memberType", "member_type")) or "").lower()
        status = (_optional_str(_first_present(member, "membershipStatus", "membership_status", "status")) or "").lower()
        if identity == member_identity and member_type == "agent" and status == "active":
            return member
    return None


def _extract_message_id(response: Any) -> int | str | None:
    return _extract_first_nested(response, "message_id", "messageId", "id")


def _extract_delivery_request_id(response: Any) -> int | str | None:
    return _extract_first_nested(
        response,
        "delivery_request_id",
        "deliveryRequestId",
        "gateway_delivery_request_id",
        "gatewayDeliveryRequestId",
        "delivery_id",
        "deliveryId",
        "request_id",
        "requestId",
    )


def _events_after_id(message_id: int | str | None) -> int:
    if message_id is None:
        return 0
    try:
        return max(0, int(message_id) - 1)
    except (TypeError, ValueError):
        return 0


def _extract_delivery_status(
    events_payload: Mapping[str, Any] | None,
    *,
    message_id: int | str | None,
    delivery_request_id: int | str | None,
    member_identity: str | None = None,
) -> str | None:
    if not isinstance(events_payload, Mapping):
        return None
    events = events_payload.get("events") or events_payload.get("data") or events_payload.get("items") or []
    if isinstance(events, Mapping):
        events = events.get("events") or events.get("items") or []
    if not isinstance(events, list):
        return None
    message_text = str(message_id) if message_id is not None else None
    delivery_text = str(delivery_request_id) if delivery_request_id is not None else None
    saw_direct_message = False
    for event in events:
        if not isinstance(event, Mapping):
            continue
        event_status = _optional_str(
            _first_present(event, "deliveryStatus", "delivery_status", "eventType", "event_type", "type", "status")
        )
        event_message_id = _optional_str(_first_present(event, "messageId", "message_id"))
        event_delivery_id = _optional_str(_first_present(event, "deliveryRequestId", "delivery_request_id", "requestId", "request_id"))
        matches_direct_message = bool(message_text and event_message_id == message_text)
        matches_delivery = bool(delivery_text and event_delivery_id == delivery_text)
        if matches_direct_message:
            saw_direct_message = True
        if matches_direct_message or matches_delivery:
            if event_status is None:
                continue
            return event_status
        if saw_direct_message and member_identity and _event_is_agent_gateway_reply(event, member_identity):
            return "agent_reply_posted"
    return None


def _event_is_agent_gateway_reply(event: Mapping[str, Any], member_identity: str) -> bool:
    sender_identity = _optional_str(_first_present(event, "senderIdentity", "sender_identity"))
    sender_type = (_optional_str(_first_present(event, "senderType", "sender_type")) or "").lower()
    source_kind = (_optional_str(_first_present(event, "sourceKind", "source_kind")) or "").lower()
    message_kind = (_optional_str(_first_present(event, "messageKind", "message_kind")) or "").lower()
    return (
        sender_identity == member_identity
        and sender_type == "agent"
        and source_kind == "gateway_delivery"
        and (not message_kind or message_kind == "agent_text")
    )


def _extract_first_nested(value: Any, *keys: str) -> int | str | None:
    if isinstance(value, Mapping):
        direct = _first_present(value, *keys)
        if direct is not None:
            return direct if isinstance(direct, (int, str)) else str(direct)
        for nested_key in ("message", "result", "data"):
            nested = value.get(nested_key)
            nested_value = _extract_first_nested(nested, *keys)
            if nested_value is not None:
                return nested_value
    return None


def _first_present(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)

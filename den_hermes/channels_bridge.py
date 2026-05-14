from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

SECRETISH_PATTERN = re.compile(r"(?i)(bearer\s+\S+|sk-[a-z0-9_.-]{8,}|api[_-]?key|auth[_-]?token|\btoken\b|authorization|password|secret)")


@dataclass(frozen=True)
class WakeResult:
    status: str
    delivery_request_id: int
    dedupe_key: str
    correlation_id: str | None
    adapter_instance_id: str | None = None
    session_id: str | None = None
    external_message_id: str | None = None
    diagnostic: str | None = None


class HermesWakeTransport(Protocol):
    def wake_profile(self, *, binding: Mapping[str, Any], envelope: Mapping[str, Any]) -> Mapping[str, Any]: ...


class InMemoryWakeStore:
    def __init__(self) -> None:
        self._records: dict[tuple[str, int, str, str], WakeResult] = {}

    def get(self, *, adapter_instance_id: str, delivery_request_id: int, dedupe_key: str, delivery_mode: str) -> WakeResult | None:
        result = self._records.get((adapter_instance_id, delivery_request_id, dedupe_key, delivery_mode))
        if result is None:
            return None
        return WakeResult(
            status="duplicate",
            delivery_request_id=result.delivery_request_id,
            dedupe_key=result.dedupe_key,
            correlation_id=result.correlation_id,
            adapter_instance_id=result.adapter_instance_id,
            session_id=result.session_id,
            external_message_id=result.external_message_id,
            diagnostic=result.diagnostic,
        )

    def put(self, *, adapter_instance_id: str, delivery_request_id: int, dedupe_key: str, delivery_mode: str, result: WakeResult) -> None:
        self._records[(adapter_instance_id, delivery_request_id, dedupe_key, delivery_mode)] = result


class DenChannelsWakeBridge:
    def __init__(self, *, den_tools: Any, hermes_transport: HermesWakeTransport, store: InMemoryWakeStore):
        self.den_tools = den_tools
        self.hermes_transport = hermes_transport
        self.store = store

    def handle_delivery(self, delivery: Mapping[str, Any]) -> WakeResult:
        delivery_request_id = _required_int(delivery, "delivery_request_id")
        dedupe_key = _required_str(delivery, "dedupe_key")
        delivery_mode = str(delivery.get("delivery_mode") or "wake")
        correlation_id = _optional_str(delivery.get("correlation_id"))
        target = _mapping(delivery.get("target"), "target")
        project_id = _required_str(target, "project_id")
        agent_identity = _required_str(target, "agent_identity")
        role = _optional_str(target.get("role"))
        if not role:
            diagnostic = f"Delivery target is missing required role for {agent_identity} in {project_id}"
            return self._fail_closed(
                delivery=delivery,
                diagnostic=diagnostic,
                failure_category="missing_target_role",
                binding_count=0,
            )

        bindings = self._find_bindings(project_id=project_id, agent_identity=agent_identity, role=role)
        if not bindings:
            diagnostic = f"No active Hermes profile binding matched {agent_identity}/{role or '*'} in {project_id}"
            return self._fail_closed(
                delivery=delivery,
                diagnostic=diagnostic,
                failure_category="missing_binding",
                binding_count=0,
            )
        missing_profile = [binding for binding in bindings if not _binding_profile(binding)]
        if missing_profile:
            adapter_instance_id = _binding_instance_id(missing_profile[0])
            diagnostic = f"Hermes profile binding {adapter_instance_id} has no profile"
            return self._fail_closed(
                delivery=delivery,
                diagnostic=diagnostic,
                failure_category="missing_profile",
                binding_count=len(bindings),
                adapter_instance_id=adapter_instance_id,
            )
        if len(bindings) > 1:
            instance_ids = ", ".join(_binding_instance_id(binding) for binding in bindings)
            diagnostic = f"Ambiguous Hermes profile binding matched {agent_identity}/{role or '*'} in {project_id}: {instance_ids}"
            return self._fail_closed(
                delivery=delivery,
                diagnostic=diagnostic,
                failure_category="ambiguous_binding",
                binding_count=len(bindings),
            )

        binding = bindings[0]
        adapter_instance_id = _binding_instance_id(binding)
        duplicate = self.store.get(
            adapter_instance_id=adapter_instance_id,
            delivery_request_id=delivery_request_id,
            dedupe_key=dedupe_key,
            delivery_mode=delivery_mode,
        )
        if duplicate is not None:
            return duplicate

        envelope = _build_delivery_envelope(delivery, binding=binding)
        try:
            wake = self.hermes_transport.wake_profile(binding=binding, envelope=envelope)
        except Exception as exc:  # noqa: BLE001 - fail closed with bounded diagnostic
            diagnostic = f"Hermes profile wake failed for {adapter_instance_id}: {_redact(str(exc))}"
            return self._fail_closed(
                delivery=delivery,
                diagnostic=diagnostic,
                failure_category="hermes_transport_failure",
                binding_count=1,
                adapter_instance_id=adapter_instance_id,
            )

        result = WakeResult(
            status="delivered",
            delivery_request_id=delivery_request_id,
            dedupe_key=dedupe_key,
            correlation_id=correlation_id,
            adapter_instance_id=adapter_instance_id,
            session_id=_optional_str(wake.get("session_id")),
            external_message_id=_optional_str(wake.get("external_message_id")),
        )
        self.store.put(
            adapter_instance_id=adapter_instance_id,
            delivery_request_id=delivery_request_id,
            dedupe_key=dedupe_key,
            delivery_mode=delivery_mode,
            result=result,
        )
        return result

    def _find_bindings(self, *, project_id: str, agent_identity: str, role: str) -> list[Mapping[str, Any]]:
        args = {"project_id": project_id, "agent_identity": agent_identity, "role": role, "status": "active,degraded"}
        response = self.den_tools.mcp_den_list_agent_instance_bindings(**args)
        raw_bindings = _extract_bindings(response)
        return [
            binding
            for binding in raw_bindings
            if _binding_transport_kind(binding) == "hermes_profile"
            and binding.get("project_id") is not None
            and str(binding.get("project_id")) == project_id
            and (binding.get("agent_identity") is not None or binding.get("agent") is not None)
            and str(binding.get("agent_identity") or binding.get("agent")) == agent_identity
            and binding.get("role") is not None
            and str(binding.get("role")) == role
            and binding.get("status") is not None
            and str(binding.get("status")) in {"active", "degraded"}
        ]

    def _fail_closed(
        self,
        *,
        delivery: Mapping[str, Any],
        diagnostic: str,
        failure_category: str,
        binding_count: int,
        adapter_instance_id: str | None = None,
    ) -> WakeResult:
        delivery_request_id = _required_int(delivery, "delivery_request_id")
        dedupe_key = _required_str(delivery, "dedupe_key")
        correlation_id = _optional_str(delivery.get("correlation_id"))
        target = _mapping(delivery.get("target"), "target")
        project_id = _required_str(target, "project_id")
        agent_identity = _required_str(target, "agent_identity")
        role = _optional_str(target.get("role"))
        safe_diagnostic = _redact(diagnostic)
        self.den_tools.mcp_den_send_agent_stream_message(
            sender="den-hermes-bridge",
            event_type="note",
            body=safe_diagnostic,
            project_id=project_id,
            task_id=_source_task_id(delivery),
            recipient_agent=agent_identity,
            recipient_role=role,
            delivery_mode="record_only",
            metadata={
                "type": "hermes_wake_diagnostic",
                "delivery_request_id": delivery_request_id,
                "dedupe_key": dedupe_key,
                "correlation_id": correlation_id,
                "adapter_instance_id": adapter_instance_id,
                "failure_category": failure_category,
                "binding_count": binding_count,
            },
            dedup_key=f"wake-diagnostic:{dedupe_key}:failed",
        )
        return WakeResult(
            status="failed",
            delivery_request_id=delivery_request_id,
            dedupe_key=dedupe_key,
            correlation_id=correlation_id,
            adapter_instance_id=adapter_instance_id,
            diagnostic=safe_diagnostic,
        )


def _build_delivery_envelope(delivery: Mapping[str, Any], *, binding: Mapping[str, Any]) -> dict[str, Any]:
    target = _sanitize_mapping(_mapping(delivery.get("target"), "target"))
    target["profile"] = _binding_profile(binding)
    target["adapter_instance_id"] = _binding_instance_id(binding)
    return {
        "type": "den_delivery",
        "schema_version": 1,
        "delivery_request_id": _required_int(delivery, "delivery_request_id"),
        "attempt_id": _optional_int(delivery.get("attempt_id")),
        "delivery_mode": str(delivery.get("delivery_mode") or "wake"),
        "dedupe_key": _required_str(delivery, "dedupe_key"),
        "correlation_id": _optional_str(delivery.get("correlation_id")),
        "target": target,
        "source": _sanitize_mapping(_mapping(delivery.get("source", {}), "source")),
        "message": _sanitize_mapping(_mapping(delivery.get("message", {}), "message")),
        "instructions": [
            "Refresh Den state before acting.",
            "Use source pointers for full context.",
            "Acknowledge the delivery if possible.",
        ],
    }


def _extract_bindings(response: Any) -> list[Mapping[str, Any]]:
    if isinstance(response, str):
        response = json.loads(response)
    if isinstance(response, list):
        return [item for item in response if isinstance(item, Mapping)]
    if isinstance(response, Mapping):
        for key in ("bindings", "items", "result"):
            value = response.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, Mapping)]
        if isinstance(response.get("result"), str):
            return _extract_bindings(response["result"])
    return []


def _binding_instance_id(binding: Mapping[str, Any]) -> str:
    value = binding.get("instance_id") or binding.get("adapter_instance_id") or binding.get("id")
    return str(value)


def _binding_transport_kind(binding: Mapping[str, Any]) -> str:
    return str(binding.get("transport_kind") or binding.get("adapter_kind") or "")


def _binding_profile(binding: Mapping[str, Any]) -> str:
    metadata = binding.get("metadata") if isinstance(binding.get("metadata"), Mapping) else {}
    value = binding.get("profile") or metadata.get("profile") or ""
    return str(value)


def _sanitize_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, item in value.items():
        key_text = str(key)
        secretish_key = SECRETISH_PATTERN.search(key_text) is not None
        safe_key = "[REDACTED_KEY]" if secretish_key else key_text
        sanitized[safe_key] = "[REDACTED]" if secretish_key else _sanitize_value(item)
    return sanitized


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _sanitize_mapping(value)
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, str):
        return _redact(value)
    return value


def _redact(value: str) -> str:
    return SECRETISH_PATTERN.sub("[REDACTED]", value)


def _source_task_id(delivery: Mapping[str, Any]) -> int | None:
    source = delivery.get("source")
    if not isinstance(source, Mapping):
        return None
    for key in ("task_id", "den_task_id"):
        if key in source:
            return _optional_int(source.get(key))
    return None


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    raise ValueError(f"{field} must be a mapping")


def _required_str(mapping: Mapping[str, Any], field: str) -> str:
    value = mapping.get(field)
    if value is None or str(value) == "":
        raise ValueError(f"{field} is required")
    return str(value)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _required_int(mapping: Mapping[str, Any], field: str) -> int:
    value = mapping.get(field)
    if value is None:
        raise ValueError(f"{field} is required")
    return int(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)

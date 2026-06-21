from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

from den_hermes.channels_bridge import SpawnedHermesProfileWakeTransport


class GatewayHttpClient:
    def __init__(self, base_url: str, *, timeout_seconds: int = 10) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def list_binding_snapshots(self) -> list[dict[str, Any]]:
        body = self._request("GET", "/api/binding-snapshots")
        if isinstance(body, Mapping):
            return list(body.get("items") or [])
        raise RuntimeError("Gateway binding snapshot response was not an object")

    def claim_deliveries(self, request: Mapping[str, Any]) -> list[dict[str, Any]]:
        body = self._request("POST", "/api/deliveries/claim", request)
        if isinstance(body, Mapping):
            return list(body.get("deliveries") or [])
        raise RuntimeError("Gateway claim response was not an object")

    def mark_delivered(self, delivery_request_id: int, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._request("POST", f"/api/deliveries/{delivery_request_id}/delivered", payload)

    def upsert_adapter_binding(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._request("PUT", "/api/adapter-bindings/heartbeat", payload)

    def mark_failed(self, delivery_request_id: int, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._request("POST", f"/api/deliveries/{delivery_request_id}/fail", payload)

    def _request(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310 - LAN service URL is configured by operator.
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:  # pragma: no cover - exercised in live smoke.
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Gateway HTTP {method} {path} failed: {exc.code} {detail[:500]}") from exc
        return json.loads(raw) if raw else {}


class GatewayDeliveryConsumer:
    def __init__(
        self,
        *,
        gateway_client: Any,
        hermes_transport: Any | None = None,
        accepted_delivery_modes: list[str] | None = None,
        claim_limit: int = 5,
        lease_seconds: int = 300,
    ) -> None:
        self.gateway_client = gateway_client
        self.hermes_transport = hermes_transport or SpawnedHermesProfileWakeTransport()
        self.accepted_delivery_modes = accepted_delivery_modes or ["wake", "notify"]
        self.claim_limit = claim_limit
        self.lease_seconds = lease_seconds

    def poll_once(self) -> dict[str, int]:
        bindings = [_normalize_binding(raw) for raw in self.gateway_client.list_binding_snapshots()]
        eligible = [binding for binding in bindings if _is_claimable_binding(binding)]
        claimed_count = 0
        delivered = 0
        failed = 0

        for binding in eligible:
            if hasattr(self.gateway_client, "upsert_adapter_binding"):
                self.gateway_client.upsert_adapter_binding(_binding_heartbeat_payload(binding))
            claim_request = {
                "adapter_kind": binding["adapter_kind"],
                "adapter_instance_id": binding["adapter_instance_id"],
                "project_id": binding.get("project_id"),
                "agent_identity": binding.get("agent_identity"),
                "role": binding.get("role"),
                "accepted_delivery_modes": self.accepted_delivery_modes,
                "limit": self.claim_limit,
                "lease_seconds": self.lease_seconds,
            }
            deliveries = self.gateway_client.claim_deliveries(claim_request)
            claimed_count += len(deliveries)
            for delivery in deliveries:
                delivery_request_id = _required_int(delivery, "delivery_request_id")
                try:
                    envelope = _delivery_to_envelope(delivery, binding)
                    wake = self.hermes_transport.wake_profile(binding=binding, envelope=envelope)
                    self.gateway_client.mark_delivered(
                        delivery_request_id,
                        {
                            "attempt_id": _optional_int(delivery.get("attempt_id")),
                            "adapter_kind": binding["adapter_kind"],
                            "adapter_instance_id": binding["adapter_instance_id"],
                            "external_message_id": _optional_str(wake.get("external_message_id")),
                            "session_id": _optional_str(wake.get("session_id")),
                        },
                    )
                    delivered += 1
                except Exception as exc:  # noqa: BLE001 - fail closed and report to Gateway.
                    self.gateway_client.mark_failed(
                        delivery_request_id,
                        {
                            "attempt_id": _optional_int(delivery.get("attempt_id")),
                            "adapter_kind": binding.get("adapter_kind"),
                            "adapter_instance_id": binding.get("adapter_instance_id"),
                            "error_code": "hermes_wake_failed",
                            "error_message": _bounded(str(exc)),
                        },
                    )
                    failed += 1

        return {
            "bindings_seen": len(eligible),
            "deliveries_claimed": claimed_count,
            "delivered": delivered,
            "failed": failed,
        }


def _normalize_binding(raw: Mapping[str, Any]) -> dict[str, Any]:
    metadata = _metadata(raw)
    return {
        "agent_identity": raw.get("agent_identity") or raw.get("agentIdentity"),
        "project_id": raw.get("project_id") or raw.get("projectId"),
        "role": raw.get("role"),
        "adapter_kind": raw.get("adapter_kind") or raw.get("adapterKind") or raw.get("transport_kind") or raw.get("transportKind"),
        "adapter_instance_id": raw.get("adapter_instance_id") or raw.get("adapterInstanceId") or raw.get("instance_id") or raw.get("instanceId"),
        "status": raw.get("status"),
        "is_stale": bool(raw.get("is_stale") or raw.get("isStale") or False),
        "metadata": metadata,
        "profile": raw.get("profile") or metadata.get("profile"),
        "pool_member_id": raw.get("pool_member_id") or raw.get("poolMemberId") or metadata.get("pool_member_id"),
        "worker_identity": raw.get("worker_identity") or raw.get("workerIdentity") or metadata.get("worker_identity"),
    }


def _binding_pool_member_id(binding: Mapping[str, Any]) -> str | None:
    """Extract the pool_member_id from a binding, if present.

    Pool member identity lives in:
      1. binding top-level "pool_member_id" field
      2. binding metadata "pool_member_id" key
      3. binding top-level "worker_identity" field
    Returns None if absent.
    """
    value = binding.get("pool_member_id")
    if value:
        return str(value)
    metadata = binding.get("metadata") or {}
    if isinstance(metadata, Mapping):
        value = metadata.get("pool_member_id")
        if value:
            return str(value)
    value = binding.get("worker_identity")
    if value:
        return str(value)
    return None


def _metadata(raw: Mapping[str, Any]) -> dict[str, Any]:
    metadata = raw.get("metadata")
    if isinstance(metadata, Mapping):
        return dict(metadata)
    metadata_json = raw.get("metadata_json") or raw.get("metadataJson")
    if isinstance(metadata_json, str) and metadata_json.strip():
        try:
            parsed = json.loads(metadata_json)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _is_claimable_binding(binding: Mapping[str, Any]) -> bool:
    return (
        binding.get("adapter_kind") == "hermes_profile"
        and bool(binding.get("adapter_instance_id"))
        and bool(binding.get("agent_identity"))
        and bool(binding.get("project_id"))
        and bool(binding.get("role"))
        and bool(binding.get("profile"))
        and str(binding.get("status") or "").lower() in {"active", "degraded"}
        and not bool(binding.get("is_stale"))
    )


def _binding_heartbeat_payload(binding: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "adapter_kind": binding.get("adapter_kind"),
        "adapter_instance_id": binding.get("adapter_instance_id"),
        "agent_identity": binding.get("agent_identity"),
        "project_id": binding.get("project_id"),
        "role": binding.get("role"),
        "status": binding.get("status") or "active",
        "capabilities_json": json.dumps({"accepted_delivery_modes": ["wake", "notify"]}, sort_keys=True),
        "metadata_json": json.dumps(binding.get("metadata") or {}, sort_keys=True),
    }


def _delivery_to_envelope(delivery: Mapping[str, Any], binding: Mapping[str, Any]) -> dict[str, Any]:
    metadata = _parsed_delivery_metadata(delivery)
    pool_member_id = _binding_pool_member_id(binding)
    target = {
        "project_id": delivery.get("project_id") or delivery.get("projectId") or binding.get("project_id"),
        "agent_identity": binding.get("agent_identity"),
        "role": binding.get("role"),
        "profile": binding.get("profile"),
        "adapter_instance_id": binding.get("adapter_instance_id"),
        "profile_identity": binding.get("profile"),
        "worker_identity": pool_member_id or binding.get("adapter_instance_id"),
    }
    if pool_member_id:
        target["pool_member_id"] = pool_member_id
    source = {
        "source_kind": delivery.get("source_kind") or delivery.get("sourceKind"),
        "source_id": delivery.get("source_id") or delivery.get("sourceId"),
        "source_project_id": delivery.get("source_project_id") or delivery.get("sourceProjectId"),
        "context_link": delivery.get("context_link") or delivery.get("contextLink"),
        "channel_id": metadata.get("channel_id"),
        "sender_identity": metadata.get("sender_identity"),
        "sender_type": metadata.get("sender_type"),
    }
    return {
        "type": "den_delivery",
        "schema_version": 1,
        "delivery_request_id": _required_int(delivery, "delivery_request_id"),
        "attempt_id": _optional_int(delivery.get("attempt_id") or delivery.get("attemptId")),
        "delivery_mode": delivery.get("delivery_mode") or delivery.get("deliveryMode") or "wake",
        "dedupe_key": delivery.get("dedupe_key") or delivery.get("dedupeKey"),
        "target": target,
        "source": source,
        "message": {
            "summary": delivery.get("context_summary") or delivery.get("contextSummary"),
            "metadata": metadata,
        },
        "reply": {
            "kind": "conversation_successor_channel_message",
            "base_url": os.environ.get("DEN_CONVERSATION_URL") or os.environ.get("DEN_GATEWAY_URL") or "http://192.168.1.10:8079",
            "channel_id": metadata.get("channel_id"),
            "endpoint_template": "/v1/conversation/channels/{channel_id}/messages",
            "sender_type": "agent",
            "sender_identity": binding.get("agent_identity"),
            "message_kind": "agent_text",
            "source_kind": "external_adapter_message",
            "source_id": str(_required_int(delivery, "delivery_request_id")),
        },
        "instructions": [
            "Refresh Den state before acting.",
            "Use the delivery source pointers and then reply in the originating Den conversation channel.",
            "For a Den conversation reply, POST JSON to {base_url}{endpoint_template} using snake_case fields: sender_type, sender_identity, body, message_kind, source_kind, source_id, source_project_id, metadata, dedupe_key. Use DEN_CONVERSATION_TOKEN for Gateway auth.",
            "Do not fall back to legacy den-channels POST /api/channels/{channel_id}/messages.",
            "Do not expose secrets or raw settings in the response.",
        ],
    }


def _parsed_delivery_metadata(delivery: Mapping[str, Any]) -> dict[str, Any]:
    raw = delivery.get("metadata_json") or delivery.get("metadataJson") or "{}"
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _required_int(mapping: Mapping[str, Any], key: str) -> int:
    value = mapping.get(key) or mapping.get(_snake_to_camel(key))
    if value is None:
        raise ValueError(f"missing required integer field {key}")
    return int(value)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _snake_to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in tail)


def _bounded(value: str, limit: int = 500) -> str:
    return value[:limit]


def run_forever(consumer: GatewayDeliveryConsumer, *, interval_seconds: float) -> None:
    while True:
        try:
            result = consumer.poll_once()
        except Exception as exc:  # noqa: BLE001 - keep long-running consumer alive across Gateway restarts.
            result = {"status": "degraded", "error": _bounded(str(exc), 200)}
        print(json.dumps(result, sort_keys=True), flush=True)
        time.sleep(interval_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Claim Den Gateway deliveries and wake Hermes profiles.")
    parser.add_argument("--gateway-url", default=os.environ.get("DEN_GATEWAY_URL", "http://127.0.0.1:5300"))
    parser.add_argument("--interval-seconds", type=float, default=float(os.environ.get("DEN_GATEWAY_CONSUMER_INTERVAL_SECONDS", "5")))
    parser.add_argument("--once", action="store_true", help="Poll once and exit instead of running forever.")
    args = parser.parse_args(argv)

    consumer = GatewayDeliveryConsumer(gateway_client=GatewayHttpClient(args.gateway_url))
    if args.once:
        print(json.dumps(consumer.poll_once(), sort_keys=True))
        return 0
    run_forever(consumer, interval_seconds=args.interval_seconds)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))

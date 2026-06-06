"""Smoke test for #1982-class packet-auditor membership failure modes.

This script exercises two scenarios using only the fakeable pool-runtime and
channels-bridge primitives (no real Den/Channels calls):

1. missing-membership: a packet-auditor delivery with no active binding
   produces a ``membership_not_active`` canonical diagnostic.

2. restored-membership: an active binding allows a normal delivery to succeed
   without 404.

Run: python scripts/smoke_packet_auditor_membership.py
"""

from __future__ import annotations

from den_hermes.channels_bridge import (
    DenChannelsWakeBridge,
    InMemoryWakeStore,
    LEGACY_TO_CANONICAL_FAILURE_CATEGORY,
)


class FakeDenTools:
    """Fake Den MCP tools that return the supplied bindings."""

    def __init__(self, bindings: list[dict]):
        self._bindings = bindings
        self.agent_stream_messages: list[dict] = []

    def mcp_den_list_agent_instance_bindings(self, **kwargs):
        # Return a flat list of raw bindings dicts.
        return self._bindings

    def mcp_den_send_agent_stream_message(self, **kwargs):
        self.agent_stream_messages.append(kwargs)


class FakeHermesTransport:
    """Fake transport that records wake attempts."""

    def __init__(self):
        self.wakes: list[dict] = []

    def wake_profile(self, *, binding, envelope):
        self.wakes.append({"binding": dict(binding), "envelope": dict(envelope)})
        return {"session_id": "smoke-session-1", "log_path": "/dev/null"}


def active_binding(instance_id: str = "auditor-inst", role: str = "packet_auditor"):
    return {
        "project_id": "den-core",
        "agent_identity": "spawned-packet-auditor",
        "role": role,
        "status": "active",
        "transport_kind": "hermes_profile",
        "instance_id": instance_id,
        "profile": "spawned-packet-auditor",
        "metadata": {"pool_member_id": "pool-packet-auditor-03"},
    }


def delivery(role: str = "packet_auditor"):
    return {
        "delivery_request_id": 2003001,
        "dedupe_key": "dedup:2003:auditor",
        "delivery_mode": "wake",
        "correlation_id": "corr-aaaa",
        "target": {
            "project_id": "den-core",
            "agent_identity": "spawned-packet-auditor",
            "role": role,
        },
        "source": {"task_id": 1982, "project_id": "den-core"},
    }


def main():
    print("=== Scenario 1: missing membership ===")

    # No bindings => membership_not_active diagnostic
    tools1 = FakeDenTools(bindings=[])
    bridge1 = DenChannelsWakeBridge(
        den_tools=tools1,
        hermes_transport=FakeHermesTransport(),
        store=InMemoryWakeStore(),
    )
    result1 = bridge1.handle_delivery(delivery())
    assert result1.status == "failed", f"expected failed, got {result1.status}"
    diag1 = tools1.agent_stream_messages[0]
    canonical = diag1["metadata"]["failure_category"]
    assert canonical == "membership_not_active", f"expected membership_not_active, got {canonical}"
    print(f"  ✓ status=failed, canonical_category={canonical}")
    print(f"  ✓ diagnostic: {result1.diagnostic[:100]}...")

    print("\n=== Scenario 2: restored membership ===")

    # Active binding => delivered
    tools2 = FakeDenTools([active_binding()])
    bridge2 = DenChannelsWakeBridge(
        den_tools=tools2,
        hermes_transport=FakeHermesTransport(),
        store=InMemoryWakeStore(),
    )
    result2 = bridge2.handle_delivery(delivery())
    assert result2.status == "delivered", f"expected delivered, got {result2.status}"
    print(f"  ✓ status=delivered, session_id={result2.session_id}")

    print("\n=== Scenario 3: canonical category mapping ===")
    for legacy, canonical in sorted(LEGACY_TO_CANONICAL_FAILURE_CATEGORY.items()):
        print(f"  {legacy} -> {canonical}")
    print(f"  ✓ {len(LEGACY_TO_CANONICAL_FAILURE_CATEGORY)} legacy categories mapped")

    print("\n=== All smoke scenarios passed ===")


if __name__ == "__main__":
    main()

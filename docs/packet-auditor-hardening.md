# Packet-auditor operational hardening (#2071)

Operational hardening for the `packet_auditor` spawned-Hermes worker role after the #1982 gate failure.

## Problem (#1982)

During `den-core #1982`, the packet-auditor gate failed operationally even though all code gates passed:

- `spawned-packet-auditor` had no active `target_work` membership in `#den-system` (channel 672)
- Wake bridge returned `member_not_active_agent` / 404
- `pool-packet-auditor-03` remained `busy` after terminal assignment (#491 expired/released)
- Packet-auditor profile logs showed expired OpenAI Codex auth during the window

## Wired hardening

### 1. Membership preflight

`PoolWorkerProfileGuide` now carries `requires_channel_membership` and `target_channel_id`:

```python
guide = PoolWorkerProfileGuide(
    role="packet_auditor",
    runtime_id="r1",
    profile="spawned-packet-auditor",
    provider="openai",
    model="gpt-5",
    requires_channel_membership=True,
    target_channel_id=672,
)
assert guide.needs_membership_preflight()  # True
```

Orchestrators should verify active membership in `#den-system` (672) before launching packet-auditor work.

### 2. Canonical failure diagnostics

`CANONICAL_FAILURE_CATEGORIES` and `PoolMemberDiagnostic` provide structured, testable diagnostic categories:

| Category | Meaning |
|----------|---------|
| `membership_not_active` | Worker's target channel membership is not active |
| `wake_route_404` | Wake bridge route returned 404 |
| `auth_unhealthy` | Profile auth/provider health check failed |
| `post_terminal_pool_state_leak` | Pool member stuck busy without active assignment |

The `DenChannelsWakeBridge` maps legacy failure categories to canonical ones:

```python
# channels_bridge.py
LEGACY_TO_CANONICAL_FAILURE_CATEGORY = {
    "missing_binding": "membership_not_active",
    "hermes_transport_failure": "wake_route_404",
    ...
}
```

### 3. Auth health check

`check_profile_health()` is a fakeable health check:

```python
def fake_auth_check(profile, provider, model):
    return (False, "expired OAuth token")

result = check_profile_health(
    profile="spawned-packet-auditor",
    provider="openai", model="gpt-5",
    health_fn=fake_auth_check,
)
# result.is_healthy() == False
# result.to_diagnostic(member_id="pool-packet-auditor-03").category == "auth_unhealthy"
```

### 4. Post-terminal cleanup reconciliation

`reconcile_pool_members()` detects busy-without-active-assignment leaks:

```python
members = [
    {"member_id": "pool-packet-auditor-03", "state": "completed", "role": "packet_auditor"},
]
leaks = reconcile_pool_members(members=members, active_assignments_by_member={})
# leaks[0].category == "post_terminal_pool_state_leak"
```

### 5. Membership wake smoke

`scripts/smoke_packet_auditor_membership.py` exercises the #1982 failure modes:
- Missing membership → `membership_not_active` canonical diagnostic
- Restored membership → `delivered` status

## Channel residency

Packet-auditor workers require active `target_work` membership in `#den-system` (channel 672) for wake to succeed. See `_global/den-system-collected-channel-routing` for the full routing decision.

## Related

- `den_hermes/pool_runtime.py` — `PoolMemberDiagnostic`, `PostTerminalBusyLeak`, `reconcile_pool_members`, `check_profile_health`
- `den_hermes/channels_bridge.py` — `LEGACY_TO_CANONICAL_FAILURE_CATEGORY`, `emit_diagnostic`
- `scripts/smoke_packet_auditor_membership.py` — #1982 failure-mode smoke

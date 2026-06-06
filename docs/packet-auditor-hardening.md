# Packet-auditor operational hardening (#2071)

Operational hardening for the `packet_auditor` spawned-Hermes worker role after the #1982 gate failure.

## Problem (#1982)

During `den-core #1982`, the packet-auditor gate failed operationally even though all code gates passed:

- `spawned-packet-auditor` had no active `target_work` membership in `#den-system` (channel 672)
- Wake bridge returned `member_not_active_agent` / 404
- `pool-packet-auditor-03` remained `busy` after terminal assignment (#491 expired/released)
- Packet-auditor profile logs showed expired OpenAI Codex auth during the window

## Enforced workflow

### 1. Membership preflight (MUST pass before assignment)

`PoolWorkerProfileGuide` sets `requires_channel_membership=True, target_channel_id=672` for the packet_auditor role. The orchestrator/runner MUST verify active membership before launching.

`provision_pool_workers.py` now emits `requires_channel_membership` and `target_channel_id` in its apply payloads so Core can enforce the requirement during pool-member registration.

### 2. Profile auth health (MUST pass before assignment)

`pre_assignment_health_check()` MUST be called before assigning work. If it returns a `PoolMemberDiagnostic` with `category="auth_unhealthy"`, the assignment MUST be blocked with `auth_required`.

```python
def fake_auth_check(profile, provider, model):
    return (False, "expired OAuth token")

guide = PoolWorkerProfileGuide(
    role="packet_auditor", profile="spawned-packet-auditor",
    provider="openai", model="gpt-5",
    requires_channel_membership=True, target_channel_id=672,
)
diag = pre_assignment_health_check(
    guide=guide, member_id="pool-packet-auditor-03",
    health_fn=fake_auth_check,
)
# diag.category == "auth_unhealthy" → BLOCK assignment
```

### 3. Post-terminal cleanup (MUST run during cleanup)

`terminal_cleanup_reconciliation()` MUST be invoked during terminal assignment cleanup. Any detected `PostTerminalBusyLeak` produces a `post_terminal_pool_state_leak` diagnostic and the member MUST be released or quarantined.

```python
members = [
    {"member_id": "pool-packet-auditor-03", "state": "completed",
     "role": "packet_auditor", "assignment_id": "assign-491"},
]
leaks, diagnostics = terminal_cleanup_reconciliation(
    members=members, active_assignments_by_member={},
)
# leaks[0].member_id == "pool-packet-auditor-03"
# → MUST release or quarantine
```

### 4. Canonical failure diagnostics

`CANONICAL_FAILURE_CATEGORIES` and `PoolMemberDiagnostic` provide structured, testable diagnostic categories:

| Category | Meaning |
|----------|---------|
| `membership_not_active` | Worker's target channel membership is not active |
| `wake_route_404` | Wake bridge route returned 404 |
| `auth_unhealthy` | Profile auth/provider health check failed |
| `post_terminal_pool_state_leak` | Pool member stuck busy without active assignment |
| `worker_claim_timeout` | Worker claim/wake timed out |

`DenChannelsWakeBridge._fail_closed` maps 7 legacy and 1 new (`worker_claim_timeout`) failure categories to canonical ones.

### 5. Membership wake smoke

`scripts/smoke_packet_auditor_membership.py` exercises the #1982 failure modes:
- Missing membership → `membership_not_active` canonical diagnostic
- Restored membership → `delivered` status
- All 8 legacy→canonical category mappings verified

## Channel residency (enforced)

Packet-auditor workers MUST have active `target_work` membership in `#den-system` (channel 672) for wake to succeed. See `_global/den-system-collected-channel-routing` for the full routing decision.

## Related

- `den_hermes/pool_runtime.py` — `PoolMemberDiagnostic`, `PostTerminalBusyLeak`, `reconcile_pool_members`, `check_profile_health`, `pre_assignment_health_check`, `terminal_cleanup_reconciliation`
- `den_hermes/channels_bridge.py` — `LEGACY_TO_CANONICAL_FAILURE_CATEGORY` (8 mappings), `emit_diagnostic`
- `scripts/provision_pool_workers.py` — `requires_channel_membership`, `target_channel_id` in payloads
- `scripts/smoke_packet_auditor_membership.py` — #1982 failure-mode smoke

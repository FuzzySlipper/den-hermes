# Worker Pool MVP rollout runbook (task #1728)

Status: rollout guidance for the worker pool MVP proof.
Parent: `den-core` #1685 worker-pool implementation plan.
Related: `den-hermes-bridge` ##1722-#1729.

## 1. Summary

The worker pool MVP adds a persistent Hermes worker pool runtime that
receives assignments via Core lease + Gateway delivery, runs through
a checkpoint protocol state machine, and releases or quarantines
deterministically.

This document covers:
- When to use worker pool vs one-shot spawned-Hermes vs direct
  `delegate_task` (Section 2).
- How to diagnose stuck leases (Section 3).
- How release/quarantine works (Section 4).
- How to observe an assignment in Den Web (Section 5).
- Constrained live-smoke runbook/checklist (Section 6).
- Operator troubleshooting (Section 7).

## 2. Substrate selection guide

Three worker substrates are available. Choose based on durability,
lifetime, and isolation requirements.

### 2.1 Direct `delegate_task`

| Attribute | Value |
| --- | --- |
| Lifetime | Parent-turn-bound |
| Durability | None — cancelled if parent turn is interrupted |
| Artifact | Summary only, no durable JSON artifact |
| Isolation | In-process (parent Hermes session) |
| Best for | Research fan-out, review helpers, quick inspection |

Use `delegate_task` for short synchronous helper work where losing
the result on parent interruption is acceptable.

### 2.2 One-shot spawned-Hermes

| Attribute | Value |
| --- | --- |
| Lifetime | Single subprocess run |
| Durability | Den-registered run with completion packet |
| Artifact | Structured JSON at deterministic path |
| Isolation | Subprocess with explicit profile/toolsets |
| Best for | Standalone coder/reviewer/validator tasks with full bridge workflow |
| Failure handling | Registration-before-spawn gate; Den completion fail-closed |

Use one-shot spawned-Hermes via `run_den_coder_reviewer_workflow`
when you need durable worker identity, per-role profiles, and
the full coder->reviewer bridge pipeline.

### 2.3 Worker pool (pool runtime)

| Attribute | Value |
| --- | --- |
| Lifetime | Persistent worker pool; per-assignment lifecycle |
| Durability | Core lease + checkpoint protocol + cleanup pipeline |
| Artifact | Structured completion packet via pool runtime |
| Isolation | Pool worker with explicit role/runtime profile |
| Best for | Recurring assignments, long-lived workers, multi-assignment pool |
| Failure handling | Checkpoint gating; incomplete cleanup => quarantine |
| Assignment flow | Core creates lease -> Gateway delivers -> Channels wakes -> worker lifecycle -> cleanup -> release/quarantine |

Use worker pool when:
- Workers should persist across multiple assignments.
- Assignment lifecycle needs explicit lease/release/quarantine.
- Gateway delivery + Channels wake infrastructure is in place.
- The checkpoint protocol is required for governance.

### 2.4 Decision flowchart

```
Is the work synchronous and short-lived (<30s)?
  YES -> delegate_task
  NO  -> Does assignment need persistent pool lifecycle (lease +
         Gateway delivery + Channels wake + cleanup pipeline)?
           YES -> worker pool (PoolWorkerRuntime)
           NO  -> one-shot spawned-Hermes
```

## 3. Diagnosing stuck leases

A "stuck lease" occurs when a Core worker-pool assignment has a
lease that has expired or been revoked, but the worker has not
detected it and continues working. Symptoms:

- Worker continues posting checkpoints but Core rejects them.
- Den Web shows the assignment in a pre-completion state (e.g.
  `interpreting` or `planning`) past the lease expiry timestamp.
- Gateway delivery record shows delivered but no callback received
  from the worker.
- Worker runtime is in a non-terminal state but the lease expiry
  has passed.

### 3.1 Detection steps

1. Check the Core assignment record:
   - Locate `lease_expires_at` on the assignment handle.
   - Compare to current time. If expired, the lease is stale.

2. Check Gateway delivery:
   - `status`: should be `delivered` if delivery succeeded.
   - `callback_received`: if `false` after lease expiry, the worker
     may be running without a valid lease.
   - `last_error`: check for delivery failure reasons.

3. Check worker state:
   - If the worker was instantiated via `PoolWorkerRuntime`, check
     its `state` and whether `is_terminal()`. A non-terminal worker
     with an expired lease is stuck.

4. Check Den Web trace:
   - `state_label`: should reflect the current gating state.
   - `lease_expired` or `failed_stale_lease` labels indicate
     lease-related problems.

### 3.2 Recovery

1. **Fail the worker**:
   - Call `runtime.fail(reason="Stale lease: Core lease expired at ...")`.
   - This moves the worker to `FAILED` (terminal).

2. **Cleanup**:
   - If cleanup evidence is complete: `runtime.cleanup(evidence)` then
     `runtime.release()`.
   - If cleanup evidence is incomplete: `runtime.quarantine()`.

3. **Core reconciliation**:
   - The assignment should be marked `failed_stale_lease` in Core.
   - A new lease can be granted for a fresh run with a new
     `assignment_id`/`run_id`.

4. **Prevention**:
   - Workers should check lease validity before posting each
     checkpoint. The pool runtime does not do this automatically;
     it is the operator's responsibility to monitor lease expiry
     and fail workers that exceed their lease.
   - Configure reasonable lease durations based on task complexity.
     A good default is 2× the expected worker runtime.

## 4. Release and quarantine

### 4.1 Release path

```
terminal state (COMPLETED | BLOCKED | FAILED)
  |
  v
cleanup(evidence)  -- requires ALL four evidence fields:
  |                   - scrub_workspace=True
  |                   - process_release=True
  |                   - session_rotation=True
  |                   - scratch_cleanup=True
  v
CLEANED_UP
  |
  v
release()
  |
  v
RELEASED  (worker can accept new assignments)
```

### 4.2 Quarantine path

```
terminal state (COMPLETED | BLOCKED | FAILED)
  |
  v
cleanup(evidence) with incomplete evidence
  |
  raises PoolCleanupError ("Cleanup evidence incomplete")
  |
  v
quarantine()  -- operator must manually inspect and resolve
  |
  v
QUARANTINED  (worker blocked from new assignments)
```

### 4.3 When quarantine is triggered

- **Incomplete cleanup evidence**: one or more required fields are
  `False`. The `missing_fields()` method reports which fields are
  absent.
- **Cleanup was never called**: `quarantine_required()` returns
  `True` when a terminal state has `cleanup_evidence is None`.
- **Infrastructure failure during cleanup**: the pool runtime
  cannot distinguish infrastructure failure from intentional
  incomplete cleanup. Both paths land in quarantine.

### 4.4 Operator resolution for quarantine

1. Inspect the quarantine reason from the assignment handle (`error`
   field, or the `PoolCleanupError` message).
2. Manually verify which cleanup actions were not performed:
   - `scrub_workspace`: worker workspace files still present.
   - `process_release`: child processes not terminated.
   - `session_rotation`: Hermes session not rotated/closed.
   - `scratch_cleanup`: temp files/dirs not removed.
3. Perform any remaining cleanup manually or via operator scripts.
4. Resume the worker from quarantine (operator-only action) or
   terminate the worker and create a fresh replacement.

### 4.5 State summary matrix

| Terminal state | Cleanup complete | Final state | Accepts new assignments? |
| --- | --- | --- | --- |
| COMPLETED | Yes | RELEASED | Yes |
| BLOCKED | Yes | RELEASED | Yes |
| FAILED | Yes | RELEASED | Yes |
| COMPLETED | No (incomplete) | QUARANTINED | No |
| BLOCKED | No (incomplete) | QUARANTINED | No |
| FAILED | No (incomplete) | QUARANTINED | No |
| COMPLETED | Never called | terminal (quarantine required) | No |
| BLOCKED | Never called | terminal (quarantine required) | No |
| FAILED | Never called | terminal (quarantine required) | No |

## 5. Observing an assignment in Den Web

An assignment in the worker pool produces a Den Web trace projection
that shows the full lifecycle at a glance. The trace is updated at
each major state transition.

### 5.1 Trace fields

| Field | Description | Example |
| --- | --- | --- |
| `assignment_id` | Unique assignment identifier | `t1728-assign-001` |
| `task_id` | Den task number | `1728` |
| `run_id` | Worker run identifier | `t1728-worker-pool-smoke` |
| `role` | Worker role | `coder` |
| `worker_id` | Pool worker name | `pool-coder-01` |
| `state_label` | Current lifecycle stage | `interpreting`, `planning`, `completed`, `released`, `quarantined` |
| `checkpoints` | Checkpoint progress | `2/0` (passed/total — total is optional in fake trace) |
| `completion_status` | Completion outcome | `completed`, `blocked`, `failed` |
| `release_status` | Release outcome | `released`, `not_released` |
| `quarantine_status` | Quarantine indicator | `quarantined` or `null` |

### 5.2 How to read the trace

1. **Open the Den Web UI** for project `den-hermes-bridge`, task
   `#1728`.
2. **Find the worker pool assignment pane** — assignments are listed
   by `assignment_id` or grouped under task `#1728`.
3. **Read the state_label**:
   - `pending`: assignment created but not delivered.
   - `lease_granted`: Core has granted a lease.
   - `delivered`: Gateway delivered the assignment.
   - `acknowledged`: worker acknowledged.
   - `interpreting` / `planning`: checkpoint active.
   - `checkpoint_approved` / `checkpoint_changes_requested` / etc.:
     checkpoint response received.
   - `completed` / `blocked` / `failed`: terminal work outcome.
   - `cleaned_up`: cleanup evidence posted.
   - `released`: worker released and available.
   - `quarantined`: worker quarantined.
4. **Check `release_status` and `quarantine_status`**:
   - `released` = green (worker can accept new assignments).
   - `quarantined` = red (worker requires operator intervention).

### 5.3 Correlation with other handles

Each assignment trace can be correlated with:
- **Core handle**: via `assignment_id` and `run_id`.
- **Gateway delivery handle**: via `delivery_id` derived from
  `assignment_id`.
- **Channels messages**: via `assignment_id` and `run_id` on the
  message record.

## 5a. No-capacity worker handling (task #1785)

The Runner/Bridge **no-capacity policy** consumes Core no-capacity
diagnostics (den-core #1780) and maps them to operator decisions,
retry/backoff behaviour, and task-thread messages.

No-capacity occurs when Core attempts to assign a worker but cannot
find an available, non-quarantined, unambiguous candidate.

### 5a.1 Core reason codes

Core reports no-capacity via these canonical ``reason_code`` strings
(defined in ``den_hermes/no_capacity_policy.py`` ``CANONICAL_REASON_CODES``):

| Core reason_code | Meaning | Bridge decision |
|---|---|---|
| ``no_matching_worker`` | No role/profile/capabilities match | ``blocked_no_role_profile`` |
| ``all_busy`` | All candidates busy | ``blocked_all_candidates_busy`` |
| ``all_quarantined_or_offline`` | All quarantined/offline | ``blocked_all_candidates_quarantined_or_offline`` |
| ``ambiguous`` | Multiple candidates, cannot select | ``blocked_ambiguous_worker_selection`` |
| ``preferred_not_found_or_busy`` | Preferred worker not ready | ``queued_waiting_for_worker`` |

Unknown or malformed codes produce ``operator_action_required_spawn_capacity``
(fail-closed, never success).

### 5a.2 Policy decisions and behaviour

| Decision string | Retry? | Operator? | Backoff | Notes |
|---|---|---|---|---|
| ``queued_waiting_for_worker`` | Yes | No | 5s, 15s, 30s, 60s, 120s, 300s | Auto-retry; cancellation available |
| ``blocked_no_role_profile`` | No | Yes | — | Verify runtime registry and role catalog |
| ``blocked_all_candidates_busy`` | Yes | No | 15s, 30s, 60s, 120s | Auto-retry; request capacity if persistent |
| ``blocked_all_candidates_quarantined_or_offline`` | No | Yes | — | Clean/reprovision quarantined workers |
| ``blocked_ambiguous_worker_selection`` | No | Yes | — | Specify concrete ``pool_member_id`` |
| ``operator_action_required_spawn_capacity`` | No | Yes | — | Inspect Core record; escalate to Patch/Planner |

### 5a.3 Queued request lifecycle

When a decision is ``queued_waiting_for_worker``, the Bridge creates a
``QueuedWaitRequest`` with a configurable TTL. The request can be:

- **Retried**: Bridge automatically retries with backoff.
- **Cancelled**: Bridge produces ``CancellationEvidence`` for
  deterministic cleanup so the request does not become a zombie.
- **Expired**: ``sweep_expired_queued_requests()`` ages out expired
  entries and emits cancellation evidence.

### 5a.4 Worker wake safety invariants

Before waking any concrete worker, the Bridge must validate the
candidate via ``validate_wake_candidate()`` (defined in the policy
module). These invariants are enforced:

- **Role match**: candidate role must match expected role.
- **No quarantined workers**: candidates with ``quarantined`` status
  or ``is_quarantined=True`` are rejected.
- **No supervisor profiles**: ``den-hermes-runner``, ``runner``,
  ``default`` are forbidden for worker wake.
- **No ambiguous bindings**: ``is_ambiguous=True`` candidates are
  rejected.
- **Idle/available only**: candidates must have status ``idle``,
  ``available``, or ``ready``.
- **No random wake**: never wake same-profile workers without a
  concrete ``pool_member_id`` — Core must have leased a concrete
  worker first.

### 5a.5 Commands quick reference

| Step | Command | Expected exit |
|---|---|---|
| Run no-capacity policy tests | ``PYTHONPATH=. python -m pytest tests/test_no_capacity_policy.py -q --tb=short`` | 0 |
| Full test suite | ``PYTHONPATH=. python -m pytest -q --tb=short`` | 0 |
| Git hygiene | ``git diff --check`` | 0 |

### 5a.6 Readback correlation

Each no-capacity diagnostic carries a ``readback_handle`` that
references the Core no-capacity record. Bridge decisions mirror
this handle so operators can correlate:

- Bridge operator_message contains ``readback_handle``.
- ``NoCapacityDecision.readback_handle`` matches Core record id.
- ``NoCapacityDiagnostic.readback_handle`` matches the same id.

### 5a.7 Module reference

The no-capacity policy lives in ``den_hermes/no_capacity_policy.py``.
Key entry points:

- ``decide_no_capacity(diagnostic)`` — primary pure-function mapper.
- ``decide_from_core_record(record)`` — convenience wrapper for raw
  Core JSON records.
- ``validate_wake_candidate(candidate, expected_role=)`` — safety
  check before waking a concrete worker.
- ``create_queued_request(diagnostic, request_id)`` — create a
  cancelleable queued request.
- ``cancel_queued_request(queued, reason=)`` — produce cancellation
  evidence.
- ``sweep_expired_queued_requests(requests, now)`` — age out expired
  queued entries.

This module is **pure and deterministic**: no I/O, no network, no
Den API calls. It only consumes Core records; it never invents or
overrides Core assignment state.

## 6. Constrained live-smoke runbook

### 6.1 Preconditions

Before any live smoke:
- [ ] Fake E2E gates pass: `python -m pytest tests/test_worker_pool_mvp_e2e.py -v`
- [ ] All existing tests pass: `python -m pytest`
- [ ] Runbook validator passes: `python scripts/validate_worker_pool_mvp_runbook.py`
- [ ] Checkpoint protocol validator passes: `python scripts/validate_checkpoint_protocol.py`
- [ ] DenOps receipt validators pass: `python scripts/validate_denops_receipt.py`
- [ ] Git hygiene: `git diff --check` reports no whitespace errors
- [ ] The smoke task is a **narrow, no-op, or docs-only** task — do not
  run live smoke on production infrastructure tasks
- [ ] The smoke assignment is disposable or uses a dedicated test
  project

### 6.2 Live-smoke procedure

Run these steps in order. Stop at any red (fail-closed) result.

#### Step 1: Assignment creation (Core)

Create a disposable pool assignment via Core. Expected outcome:

- Core returns `assignment_id`, `task_id`, `run_id`, `role`.
- Core records status `lease_granted` with a `lease_expires_at`
  timestamp.
- Hold onto the `assignment_id` and `run_id` for later readback.

#### Step 2: Gateway delivery

Trigger Gateway to deliver the assignment to a pool worker. Expected
outcome:

- Gateway returns a `delivery_id`.
- Delivery `status` is `delivered` or `pending`.
- `attempt_count` is reported.

If delivery status is `failed`, stop. Record the `last_error`.

#### Step 3: Channels wake

Verify that a Channels direct-agent message (wake) was sent to the
target worker. Expected outcome:

- A Channels message record exists with `message_type: wake` for
  the assignment `run_id`.
- Message `status` is `sent`.

If wake status is `failed`, the smoke can continue — the worker
may still pick up the assignment via polling (depending on pool
configuration).

#### Step 4: Worker acknowledges

The pool worker should pick up the assignment and call
`acknowledge(...)`. Expected outcome:

- Worker state transitions to `ACKNOWLEDGED`.
- Core assignment `status` becomes `acknowledged`.
- Den Web trace shows `state_label: acknowledged`.

If worker does not acknowledge within a reasonable timeout (e.g.
60 seconds), consider the assignment stuck. Go to Section 6.3
(fail-closed criteria).

#### Step 5: Interpretation checkpoint

The worker should post an `interpretation_checkpoint`. Expected
outcome:

- Worker state transitions to `INTERPRETING`.
- Core shows `latest_checkpoint_type: interpretation_checkpoint`.
- A Channels message with `message_type: checkpoint` is recorded.
- Den Web trace shows `state_label: interpreting`.

#### Step 6: Runner checkpoint response (approve)

Post a `checkpoint_response` with `verdict=approved`. Expected
outcome:

- Worker state transitions to `INTERPRETATION_APPROVED`.
- Core shows `latest_checkpoint_verdict: approved`.

#### Step 7: Worker posts plan checkpoint

If the worker implements this step, expect:

- Worker state transitions to `PLANNING`.
- Core shows `latest_checkpoint_type: plan_checkpoint`.
- Den Web trace shows `state_label: planning`.

#### Step 8: Runner approves plan

Post `checkpoint_response` with `verdict=approved`. Expected
outcome:

- Worker state transitions to `PLAN_APPROVED`.
- Then proceeds to `IMPLEMENTING` if it calls
  `proceed_to_implementation()`.

#### Step 9: Worker completes

The worker should call `complete(...)` and
`finalize_completion()`. Expected outcome:

- Worker state: `COMPLETED`.
- Core `completion_status`: `completed`.
- Den Web trace: `state_label: completed`, `completion_status: completed`.

#### Step 10: Cleanup and release

Trigger cleanup with complete evidence and then release. Expected
outcome:

- Worker state: `CLEANED_UP` then `RELEASED`.
- Core `status`: `released`.
- Den Web trace: `release_status: released`, `state_label: released`.
- Worker `can_accept_assignments()` returns `True`.

#### Step 11: Readback verification

For each step above, collect these readback handles:

| Handle | Source | Required fields |
| --- | --- | --- |
| Core assignment | Core API | `assignment_id`, `task_id`, `run_id`, `status`, `lease_expires_at`, `checkpoint_count`, `latest_checkpoint_type`, `completion_status` |
| Gateway delivery | Gateway API | `delivery_id`, `status`, `attempt_count`, `callback_received` |
| Channels messages | Channels API | `message_id`, `message_type`, `status` (one per wake/checkpoint message) |
| Den Web trace | Den Web UI | `assignment_id`, `state_label`, `checkpoints`, `release_status`, `quarantine_status`, `completion_status` |
| Final worker state | Pool runtime | `state`, `is_terminal()`, `is_success()`, `is_failed()`, `can_accept_assignments()` |

#### Step 12: Clean up smoke artifacts

- Remove any smoke logs or temp files from `/tmp/den-hermes/<run_id>/`.
- If a disposable smoke task was created, close it or mark it as
  smoke-complete in Den.
- Record smoke lesson fields (Section 6.4).

### 6.3 Fail-closed criteria

Stop immediately if any of these occur:

- **RED**: Gateway delivery returns `failed` or `delivery_mismatch`
  — the assignment will never reach the worker.
- **RED**: Worker acknowledge does not happen within the timeout
  — the worker may not be running or the assignment was lost.
- **RED**: Core rejects a checkpoint (wrong type, mismatched identity)
  — this is a protocol violation; do not continue.
- **RED**: Cleanup raises `PoolCleanupError` — do NOT call
  `release()`; call `quarantine()` and inspect.
- **RED**: Den Web trace shows `quarantined` when status should be
  `released` — the assignment needs manual operator review.
- **AMBER**: Channels wake fails but delivery succeeded — the worker
  may still poll eventually but the smoke is partial; note in
  lesson fields.
- **AMBER**: One or more readback handles are missing fields — not a
  blocker for smoke but indicates a projection gap.

### 6.4 Smoke lesson fields

After live smoke (or after each failed step), capture these fields
for den-core #1685:

| Field | Description | Example |
| --- | --- | --- |
| `lesson_id` | Deterministic or supplied handle | `wp-mvp-smoke-1728-20260529` |
| `lesson.source` | Smoke source identifier | `worker-pool-mvp-proof-smoke` |
| `lesson.verdict` | `passed`, `partial`, `blocked`, or `failed` | `passed` |
| `lesson.timestamp` | ISO 8601 timestamp | `2026-05-29T14:00:00Z` |
| `lesson.run_id` | The smoke pool run ID | `t1728-worker-pool-smoke` |
| `lesson.assignment_id` | Core assignment ID | `t1728-assign-001` |
| `lesson.final_worker_state` | Final PoolRuntimeState value | `released` |
| `lesson.core_status` | Core assignment status | `released` |
| `lesson.notes` | Free-text observations | `All 12 steps completed. Delivery callback received.` |
| `lesson.fail_step` | Step number where failure occurred | `null` if passed |
| `lesson.fail_reason` | Why the smoke stopped | `null` if passed |

### 6.5 Example lesson record (passed)

```json
{
  "lesson_id": "wp-mvp-smoke-1728-20260529",
  "lesson.source": "worker-pool-mvp-proof-smoke",
  "lesson.verdict": "passed",
  "lesson.timestamp": "2026-05-29T14:00:00Z",
  "lesson.run_id": "t1728-worker-pool-smoke",
  "lesson.assignment_id": "t1728-assign-001",
  "lesson.final_worker_state": "released",
  "lesson.core_status": "released",
  "lesson.notes": "All 12 steps completed. Delivery callback received. No quarantine.",
  "lesson.fail_step": null,
  "lesson.fail_reason": null
}
```

### 6.6 Example lesson record (failed at delivery)

```json
{
  "lesson_id": "wp-mvp-smoke-1728-20260529",
  "lesson.source": "worker-pool-mvp-proof-smoke",
  "lesson.verdict": "blocked",
  "lesson.timestamp": "2026-05-29T14:00:00Z",
  "lesson.run_id": "t1728-worker-pool-smoke",
  "lesson.assignment_id": "t1728-assign-001",
  "lesson.final_worker_state": null,
  "lesson.core_status": "delivery_failed",
  "lesson.notes": "Gateway delivery failed after 3 retries. Worker never acknowledged.",
  "lesson.fail_step": 2,
  "lesson.fail_reason": "Gateway returned status=failed after 3 attempts; last_error='Worker unreachable'"
}
```

## 7. Live pool worker provisioning (task #1784)

The live pool worker provisioning step registers concrete pool members for
the four roles: **reviewer**, **validator**, **drift_checker**, and
**packet_auditor**. (Coder is already operational from prior work.)

### 7.1 Role profile matrix

All live roles use **shared spawned-Hermes profiles** (`spawned-*`) — never
`den-hermes-runner` or other operator-first profiles. Concrete instance
identity comes from `pool_member_id` / `agent_instance_id`, not from
duplicate profile names.  See `docs/spawned-role-pool-member-identity.md`.

| Role | Profile identity | Pool member ID | Den core role | Packet type | Runtime ID |
|---|---|---|---|---|---|
| Reviewer | `spawned-reviewer` | `pool-reviewer-01` | `reviewer` | `review_findings_packet` | `reviewer-primary` |
| Validator | `spawned-validator` | `pool-validator-01` | `validator` | `validation_packet` | `validator-primary` |
| Drift Checker | `spawned-drift-checker` | `pool-drift-checker-01` | `drift_checker` | `drift_check_packet` | `drift-checker-primary` |
| Packet Auditor | `spawned-packet-auditor` | `pool-packet-auditor-01` | `packet_auditor` | `packet_audit_packet` | `packet-auditor-primary` |

### 7.2 Preconditions

Before running live provisioning:

- [ ] Central registry at `/home/agents/runtime/spawned-hermes-runtimes.yaml`
  has the four roles configured with `spawned-*` profiles (not
  `den-hermes-runner`).
- [ ] Hermes profiles `spawned-reviewer`, `spawned-validator`,
  `spawned-drift-checker`, and `spawned-packet-auditor` exist and pass
  preflight.
- [ ] The provisioning script is committed: `scripts/provision_pool_workers.py`.
- [ ] Smoke helper is committed: `scripts/smoke_pool_worker_assignment.py`.
- [ ] Runner has Den MCP/Core access to upsert pool members (if using
  `--apply` mode).

### 7.3 Provisioning commands

#### Step P1: Validate the runtime registry

```bash
# Validate all five canonical roles resolve correctly
python -m den_hermes.runtime_ops validate --registry /home/agents/runtime/spawned-hermes-runtimes.yaml

# Show the full runtime matrix
python -m den_hermes.runtime_ops matrix --registry /home/agents/runtime/spawned-hermes-runtimes.yaml
```

Expected output includes all roles with `spawned-*` profiles.  If any
role shows `den-hermes-runner`, stop and update the registry first.

#### Step P2: Run the provisioning dry-run

```bash
# Dry-run against the central registry (default path)
python scripts/provision_pool_workers.py

# Or specify a registry path explicitly
python scripts/provision_pool_workers.py --registry /home/agents/runtime/spawned-hermes-runtimes.yaml

# Specific roles only
python scripts/provision_pool_workers.py --roles reviewer,validator

# JSON output for programmatic consumption
python scripts/provision_pool_workers.py --json
```

The dry-run validates:
- Registry schema and required roles.
- All four live roles use `spawned-*` profiles.
- No `den-hermes-runner` or other forbidden profiles leak through.
- No secret-like values (API keys, tokens) are present in the registry.

Expected outcome: `Resolved: 4 roles  Failed: 0 roles`.

#### Step P3: Credential/config guard check

```bash
# The provisioning script automatically checks for secret patterns.
# Run with --json to get structured credential_guard_ok field.
python scripts/provision_pool_workers.py --json | python -c "import sys,json; d=json.load(sys.stdin); print('GUARD:', 'PASS' if d['credential_guard_ok'] else 'FAIL'); [print(f'  SECRET: {s}') for s in d.get('secrets_found',[])]"
```

If `credential_guard_ok` is `False`, inspect the flagged config entries
manually.  Redact or remove actual secrets from the registry before
proceeding.

#### Step P4: Run the assignment smoke helper

```bash
# Smokes all four live roles (in-memory, no mutations)
python scripts/smoke_pool_worker_assignment.py

# JSON output with handles
python scripts/smoke_pool_worker_assignment.py --json

# Specific roles
python scripts/smoke_pool_worker_assignment.py --roles reviewer,validator

# Custom run-id for tracing
python scripts/smoke_pool_worker_assignment.py --run-id t1784-provision-smoke-20260530
```

The smoke helper tests:
- Assignment pointer validation (all four roles).
- PoolWorkerRuntime creation from `PENDING` state.
- `acknowledge()` transition to `ACKNOWLEDGED`.
- Role-specific packet type expectations.
- Pool member identity conventions (`pool-{role}-01`).

Expected outcome: `Roles passed: 4  Roles failed: 0`.

#### Step P5: Core readback test

After running the smoke, Runner should verify Core can read back the
assignment handles.  Use the `run_id` and `assignment_id` from the smoke
output (or from the `--run-id` override) to check:

```bash
# Example: Run smoke with a known run-id, then verify via Core
python scripts/smoke_pool_worker_assignment.py --run-id t1784-provision-validation

# Expected smoke output (abbreviated):
# POOL MEMBER          | STATE            | PACKET TYPE                   | STATUS    | RUN_ID
# pool-reviewer-01     | acknowledged     | review_findings_packet        | PASS      | t1784-provision-validation
# pool-validator-01    | acknowledged     | validation_packet             | PASS      | t1784-provision-validation
# pool-drift-checker-01| acknowledged     | drift_check_packet            | PASS      | t1784-provision-validation
# pool-packet-auditor-01| acknowledged    | packet_audit_packet           | PASS      | t1784-provision-validation
```

#### Step P6: Core upsert (apply mode)

If Runner has Den MCP/Core access, use `--apply` to emit structured JSON
payloads for upserting pool members:

```bash
# Dry-run first, then pipe apply payloads to Core
python scripts/provision_pool_workers.py --apply

# Each payload is tagged with "### DEN_MCP_UPSERT" for easy filtering:
# ### DEN_MCP_UPSERT {
#   "action": "upsert_pool_member",
#   "payload": {
#     "pool_member_id": "pool-reviewer-01",
#     "worker_role": "reviewer",
#     ...
#   }
# }
```

Pipe or feed these payloads to the Den MCP/Core upsert endpoint.  After
upsert, verify pool-members appear in Core agent-instance bindings.

#### Step P7: #worker-pool readback

Verify that the new pool members appear in the `#worker-pool` lobby
presence (Channels membership).  Each member should be keyed by
`concrete_identity` (`pool_member_id`):

```json
{
  "channel": "#worker-pool",
  "presence": [
    {
      "pool_member_id": "pool-reviewer-01",
      "agent_identity": "spawned-reviewer",
      "status": "available"
    },
    {
      "pool_member_id": "pool-validator-01",
      "agent_identity": "spawned-validator",
      "status": "available"
    },
    {
      "pool_member_id": "pool-drift-checker-01",
      "agent_identity": "spawned-drift-checker",
      "status": "available"
    },
    {
      "pool_member_id": "pool-packet-auditor-01",
      "agent_identity": "spawned-packet-auditor",
      "status": "available"
    }
  ]
}
```

#### Step P8: Same-profile ambiguity fail-closed check

If multiple bindings exist for the same profile identity (e.g. two
bindings for `spawned-reviewer`), verify that deliveries without a
`concrete_identity` or `pool_member_id` fail closed with
`ambiguous_binding`.

```bash
# Simulated check (conceptual — no automation script for this yet):
#
# 1. Register two bindings for spawned-reviewer:
#    - instance_id=hermes:den-k8:spawned-reviewer:pool-reviewer-01:wake-aaa
#      pool_member_id=pool-reviewer-01
#    - instance_id=hermes:den-k8:spawned-reviewer:pool-reviewer-02:wake-bbb
#      pool_member_id=pool-reviewer-02
# 2. Send a delivery target with agent_identity=spawned-reviewer but NO
#    pool_member_id or concrete_identity.
# 3. Expected result: Bridge returns ambiguous_binding, NOT silently
#    picking the first match.
```

All existing pool-member bindings for the same role must carry a
`pool_member_id`.  Remove or quarantine stale pilot bindings that lack
a concrete identity — they will cause ambiguity failures for new
deliveries.

### 7.4 Non-live roles: Scout

Scout remains **deferred** (design/contract only, task #1691).  It is
not yet in `CANONICAL_ROLES` in `den_hermes/runtime_registry.py`.  Do not
attempt to launch Scout pool workers until:

1. `scout` is added to `CANONICAL_ROLES`.
2. A `spawned-scout` Hermes profile exists with read-only toolset.
3. The central runtime registry has a live Scout entry (the current sample
   entry is for documentation/reference only).
4. The orchestrator has `START_SCOUT` / `AWAIT_SCOUT` action types.

### 7.5 Commands quick reference

| Step | Command | Expected exit |
|---|---|---|
| Validate registry | `python -m den_hermes.runtime_ops validate` | 0 |
| Show matrix | `python -m den_hermes.runtime_ops matrix` | 0 |
| Preflight roles | `python -m den_hermes.runtime_ops preflight --roles reviewer,validator,drift_checker,packet_auditor` | 0 |
| Provision dry-run | `python scripts/provision_pool_workers.py` | 0 |
| Provision apply | `python scripts/provision_pool_workers.py --apply` | 0 |
| Smoke assignment | `python scripts/smoke_pool_worker_assignment.py` | 0 |
| Validate role catalog | `python scripts/validate_role_catalog.py` | 0 |
| Git hygiene | `git diff --check` | 0 |

## 8. Operator troubleshooting

### 8.1 "Assignment not delivered" — Gateway shows `failed` or `pending`

Check:
- Is the pool worker process running? If not, start it.
- Is the Gateway configuration pointing to the correct worker endpoint?
- Are delivery retries configured? The fake E2E allows 3 retries
  before failing.

### 8.2 "Worker won't acknowledge" — worker running but no ack

Check:
- Is the assignment identity valid? A mismatched `assignment_id` or
  `run_id` causes fail-closed rejection.
- Is the worker in the `PENDING` state? Only `PENDING` can acknowledge.
  If the worker was used for a previous assignment and not released,
  it may still be in a terminal state.
- Check `can_accept_assignments()` — only `RELEASED` workers can accept
  new work.

### 8.3 "Checkpoint rejected" — Core says wrong type or mismatched identity

Check:
- Does the checkpoint `assignment_id` and `run_id` match the
  assignment record?
- Is the checkpoint type valid for the current state? See the allowed
  transitions in `PoolWorkerRuntime._require_state()`.
- Is the checkpoint `type` in `CANONICAL_CHECKPOINT_TYPES`?

### 8.4 "Cleanup failed" — `PoolCleanupError` raised

Check:
- Which fields are missing? Run `CleanupEvidence.missing_fields()`.
- The four required fields are: `scrub_workspace`, `process_release`,
  `session_rotation`, `scratch_cleanup`.
- If any field is `False`, cleanup raises and the worker must be
  quarantined.
- In production, ensure your cleanup implementation sets all four
  fields to `True` before calling `cleanup()`.

### 8.5 "Worker stuck in terminal state" — no release or quarantine

Check:
- Is `quarantine_required()` returning `True`? If so, the worker
  reached a terminal state but cleanup was never called.
- Call `cleanup(evidence)` with complete evidence, then either
  `release()` or `quarantine()`.

### 8.6 "Den Web trace shows wrong state" — projection out of sync

Check:
- Did the assignment progress through the correct timeline? The
  `timeline_states` array in the evidence artifact records every
  state transition.
- The `state_label` is updated at each major transition. If a step
  was skipped (e.g. a runner checkpoint response was never sent),
  the trace will reflect the last stable state.

## Appendix A: State transition reference

```
PENDING -> acknowledge() -> ACKNOWLEDGED
ACKNOWLEDGED -> post_interpretation() -> INTERPRETING
INTERPRETING -> receive_checkpoint_response(approved) -> INTERPRETATION_APPROVED
INTERPRETATION_APPROVED -> post_plan() -> PLANNING
PLANNING -> receive_checkpoint_response(approved) -> PLAN_APPROVED
PLAN_APPROVED -> proceed_to_implementation() -> IMPLEMENTING
IMPLEMENTING -> post_partial_result() -> PARTIAL_RESULT
PARTIAL_RESULT -> receive_checkpoint_response(approved) -> PARTIAL_RESULT_APPROVED
PARTIAL_RESULT_APPROVED -> continue_implementation() -> IMPLEMENTING
IMPLEMENTING -> complete() -> COMPLETING
COMPLETING -> finalize_completion() -> COMPLETED
COMPLETED|BLOCKED|FAILED -> cleanup() -> CLEANED_UP
CLEANED_UP -> release() -> RELEASED
CLEANED_UP|FAILED|BLOCKED -> quarantine() -> QUARANTINED
(any non-terminal) -> block() -> BLOCKED
(any non-terminal) -> fail() -> FAILED
INTERPRETING -> receive_checkpoint_response(changes_requested) -> ACKNOWLEDGED
PLANNING -> receive_checkpoint_response(changes_requested) -> INTERPRETATION_APPROVED
PARTIAL_RESULT -> receive_checkpoint_response(changes_requested) -> IMPLEMENTING
INTERPRETING|PLANNING|PARTIAL_RESULT -> receive_checkpoint_response(blocked) -> BLOCKED
BLOCKED_NEEDS_INPUT -> receive_checkpoint_response(approved|changes_requested) -> ACKNOWLEDGED
```

## Appendix B: Required fields for the fake E2E evidence shape

The evidence artifact produced by a deterministic fake E2E scenario
contains these fields for each durable handle:

### Core handle
- `assignment_id` (string)
- `task_id` (int)
- `run_id` (string)
- `role` (string)
- `status` (string)
- `project_id` (string | null)
- `lease_expires_at` (string | null)
- `checkpoint_count` (int)
- `latest_checkpoint_type` (string | null)
- `latest_checkpoint_verdict` (string | null)
- `completion_status` (string | null)
- `worker_id` (string | null)
- `has_error` (bool)

### Gateway delivery handle
- `delivery_id` (string)
- `assignment_id` (string)
- `target_worker` (string)
- `status` (string)
- `attempt_count` (int)
- `callback_received` (bool)
- `callback_status` (string | null)

### Channels message handle
- `message_id` (int)
- `channel_id` (int)
- `message_type` (string)
- `status` (string)
- `assignment_id` (string | null)
- `run_id` (string | null)

### Den Web trace handle
- `assignment_id` (string)
- `task_id` (int)
- `run_id` (string)
- `role` (string)
- `worker_id` (string | null)
- `state_label` (string)
- `checkpoints` (string — e.g. "2/0")
- `completion_status` (string | null)

## Appendix C: Downstream notes for task #1739

### Pool-member identity naming convention

Spanned by task #1767.  Concrete uniqueness for shared spawned-Hermes role
profiles comes from `pool_member_id` / `agent_instance_id` — never from
duplicate Hermes profile names.

**Identity fields in Core/Channels/Gateway:**

| Layer | Shared (profile_identity) | Concrete (worker_identity) |
|---|---|---|
| Core binding | `agent_identity` (e.g. `spawned-coder`) | `instance_id` + optional `pool_member_id` |
| Channels lobby | `agent_identity` | `agent_instance_id` / `pool_member_id` |
| Gateway delivery target | `agent_identity` | `pool_member_id` / `concrete_identity` |
| Bridge envelope | `profile_identity` | `worker_identity` / `pool_member_id` |
| Hermes transport env | `DEN_HERMES_PROFILE` | `DEN_HERMES_POOL_MEMBER_ID` |

**Concrete matching in DenChannelsWakeBridge:**

1. When `len(bindings) > 1` and delivery has `pool_member_id`,
   `concrete_identity`, or `agent_instance_id`, filter bindings to
   matching `instance_id` or `pool_member_id`.
2. Exactly one match → proceed.  Zero matches → `concrete_binding_not_found`.
3. No concrete target with multiple bindings → `ambiguous_binding` (fail closed).

**First pilot role:** `spawned-coder` with pool member IDs like
`pool-coder-01`, `pool-coder-02`, etc.

**Upcoming work (#1739):**
- Complete PoolWorkerRuntime integration with concrete identity and
  pool_member_id propagation through checkpoint lifecycle.
- Registry-backed pool-worker registration with `pool_member_id` in
  Core agent_instance_bindings metadata.
- Gateway consumer heartbeat payload includes `pool_member_id`.
- Lobby presence for pool members in Channels #worker-pool keyed by
  `concrete_identity = pool_member_id??agent_instance_id??''`.

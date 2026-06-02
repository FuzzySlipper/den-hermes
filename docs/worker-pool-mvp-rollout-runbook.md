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
|- Constrained live-smoke runbook/checklist (Section 6).
|- Live pool worker provisioning (Section 7).
|- Pilot and test member lifecycle (Section 8).
|- Green-path workflow (Section 9).
|- Ownership boundaries (Section 10).
|- First-real-task checklist (Section 11).
|- Operator troubleshooting (Section 12).
|- Reconciling stale active assignments (Section 13).
|- Project-duration orchestrator pool leases (Section 14).

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

### 2.4 Target-work vs runtime/control attribution

Worker-pool records should default to Den-facing work identity before Hermes
runtime implementation details:

- `target_project_id`, `task_id`, `assignment_id`, `run_id`, `role`, and
  `pool_member_id` identify the work being served and belong in the default
  operator-visible trace.
- `runtime_project_id`, control channel id, Hermes profile, provider/model,
  session key, and bridge instance identify the transport/runtime and belong in
  metadata or diagnostics unless the operator is explicitly debugging runtime
  delivery.
- The shared `den-hermes-bridge` control channel is not the job site. A pooled
  worker can be reached through Bridge while doing work for `goblinbench`,
  `den-channels`, or any other target project; default grouping should make the
  target project/task/run obvious.

### 2.5 Decision flowchart

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

## 7. Live pool worker provisioning (tasks #1784/#1883)

The live pool worker provisioning step registers concrete pool-member slots
for all live roles. The default bounded #1883 target is **five** slots for the
high-demand **coder** and **reviewer** roles, and **three** slots for every
other live role: **validator**, **drift_checker**, **packet_auditor**, and
**project_orchestrator**.

This is an operator-approved expansion path, not autoscaling. `all_busy` means
queue/backoff/retry by default; operators may run this provisioning path to add
bounded audited slots. New slots reuse the existing shared `spawned-*` profile
identity and must not create duplicate Hermes profile directories.

### 7.1 Role profile matrix

All live roles use **shared spawned-Hermes profiles** (`spawned-*`) — never
`den-hermes-runner` or other operator-first profiles. Concrete instance
identity comes from `pool_member_id` / `agent_instance_id`, not from
duplicate profile names.  See `docs/spawned-role-pool-member-identity.md`.

| Role | Profile identity | Default pool member IDs | Den core role | Packet type | Runtime ID |
|---|---|---|---|---|---|
| Coder | `spawned-coder` | `pool-coder-01` … `pool-coder-05` | `coder` | `implementation_packet` | `coder-primary` |
| Reviewer | `spawned-reviewer` | `pool-reviewer-01` … `pool-reviewer-05` | `reviewer` | `review_findings_packet` | `reviewer-primary` |
| Validator | `spawned-validator` | `pool-validator-01` … `pool-validator-03` | `validator` | `validation_packet` | `validator-primary` |
| Drift Checker | `spawned-drift-checker` | `pool-drift-checker-01` … `pool-drift-checker-03` | `drift_checker` | `drift_check_packet` | `drift-checker-primary` |
| Packet Auditor | `spawned-packet-auditor` | `pool-packet-auditor-01` … `pool-packet-auditor-03` | `packet_auditor` | `packet_audit_packet` | `packet-auditor-primary` |
| Project Orchestrator | `spawned-orchestrator` | `pool-orchestrator-01` … `pool-orchestrator-03` | `project_orchestrator` | `orchestration_packet` | `project-orchestrator-primary` |

### 7.2 Preconditions

Before running live provisioning:

- [ ] Central registry at `/home/agents/runtime/spawned-hermes-runtimes.yaml`
  has all six live roles configured with `spawned-*` profiles (not
  `den-hermes-runner`).
- [ ] Hermes profiles `spawned-coder`, `spawned-reviewer`,
  `spawned-validator`, `spawned-drift-checker`, `spawned-packet-auditor`,
  and `spawned-orchestrator` exist and pass preflight.
- [ ] The provisioning script is committed: `scripts/provision_pool_workers.py`.
- [ ] Smoke helper is committed: `scripts/smoke_pool_worker_assignment.py`.
- [ ] Runner has Den MCP/Core access to upsert pool members (if using
  `--apply` mode).

### 7.3 Provisioning commands

#### Step P1: Validate the runtime registry

```bash
# Validate all six canonical live roles resolve correctly
python -m den_hermes.runtime_ops --registry /home/agents/runtime/spawned-hermes-runtimes.yaml validate

# Show the full runtime matrix
python -m den_hermes.runtime_ops --registry /home/agents/runtime/spawned-hermes-runtimes.yaml matrix
```

Expected output includes all roles with `spawned-*` profiles.  If any
role shows `den-hermes-runner`, stop and update the registry first.

#### Step P2: Run the provisioning dry-run

```bash
# Dry-run against the central registry (default path) using the #1883 target:
# coder=5, reviewer=5, all other live roles=3
python scripts/provision_pool_workers.py

# Or specify a registry path explicitly
python scripts/provision_pool_workers.py --registry /home/agents/runtime/spawned-hermes-runtimes.yaml

# Specific roles only still use each role's default slot target
python scripts/provision_pool_workers.py --roles coder,reviewer

# Operator-approved override for a bounded expansion/reduction
python scripts/provision_pool_workers.py --slot-counts coder=6,reviewer=5,validator=3,drift_checker=3,packet_auditor=3,project_orchestrator=3

# JSON output for programmatic consumption
python scripts/provision_pool_workers.py --json
```

The dry-run validates:
- Registry schema and required roles.
- All six live roles use `spawned-*` profiles.
- New slot rows share the role `profile_identity`; no duplicate Hermes
  profile directories are created for `pool-coder-03`, `pool-reviewer-05`, etc.
- No `den-hermes-runner` or other forbidden profiles leak through.
- No secret-like values (API keys, tokens) are present in the registry.

Expected default outcome: `Resolved: 6 roles  Failed: 0 roles`, with 22
concrete pool-member slots:
`pool-coder-01..05`, `pool-reviewer-01..05`, `pool-validator-01..03`,
`pool-drift-checker-01..03`, `pool-packet-auditor-01..03`, and
`pool-orchestrator-01..03`.

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
# Smokes all five task-worker roles (in-memory, no mutations).
# Project-orchestrator slots use the project-duration lease smoke pattern above,
# not AssignmentPointer/PoolWorkerRuntime task-worker smoke.
python scripts/smoke_pool_worker_assignment.py

# JSON output with handles
python scripts/smoke_pool_worker_assignment.py --json

# Specific roles
python scripts/smoke_pool_worker_assignment.py --roles coder,reviewer,validator

# Custom run-id and concrete slot for tracing a non-01 lane
python scripts/smoke_pool_worker_assignment.py --run-id t1883-provision-smoke-20260602 --slot-number 3
```

The smoke helper tests:
- Assignment pointer validation (all five task-worker roles).
- PoolWorkerRuntime creation from `PENDING` state.
- `acknowledge()` transition to `ACKNOWLEDGED`.
- Role-specific packet type expectations.
- Pool member identity conventions (`pool-{role}-NN`).

Expected outcome: `Roles passed: 5  Roles failed: 0`.

#### Step P5: Core readback test

After running the smoke, Runner should verify Core can read back the
assignment handles.  Use the `run_id` and `assignment_id` from the smoke
output (or from the `--run-id` override) to check:

```bash
# Example: Run smoke with a known run-id, then verify via Core
python scripts/smoke_pool_worker_assignment.py --run-id t1784-provision-validation

# Expected smoke output (abbreviated):
# POOL MEMBER           | STATE            | PACKET TYPE                   | STATUS    | RUN_ID
# pool-coder-01         | acknowledged     | implementation_packet         | PASS      | t1784-provision-validation
# pool-reviewer-01      | acknowledged     | review_findings_packet        | PASS      | t1784-provision-validation
# pool-validator-01     | acknowledged     | validation_packet             | PASS      | t1784-provision-validation
# pool-drift-checker-01 | acknowledged     | drift_check_packet            | PASS      | t1784-provision-validation
# pool-packet-auditor-01| acknowledged     | packet_audit_packet           | PASS      | t1784-provision-validation
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
      "pool_member_id": "pool-coder-01",
      "agent_identity": "spawned-coder",
      "status": "available"
    },
    {
      "pool_member_id": "pool-reviewer-05",
      "agent_identity": "spawned-reviewer",
      "status": "available"
    },
    {
      "pool_member_id": "pool-validator-03",
      "agent_identity": "spawned-validator",
      "status": "available"
    },
    {
      "pool_member_id": "pool-drift-checker-03",
      "agent_identity": "spawned-drift-checker",
      "status": "available"
    },
    {
      "pool_member_id": "pool-packet-auditor-03",
      "agent_identity": "spawned-packet-auditor",
      "status": "available"
    },
    {
      "pool_member_id": "pool-orchestrator-03",
      "agent_identity": "spawned-orchestrator",
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
#    - instance_id=hermes:den-k8plus:spawned-reviewer:pool-reviewer-01:wake-aaa
#      pool_member_id=pool-reviewer-01
#    - instance_id=hermes:den-k8plus:spawned-reviewer:pool-reviewer-02:wake-bbb
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

## 12. Operator troubleshooting

### 12.1 "Assignment not delivered" — Gateway shows `failed` or `pending`

Check:
- Is the pool worker process running? If not, start it.
- Is the Gateway configuration pointing to the correct worker endpoint?
- Are delivery retries configured? The fake E2E allows 3 retries
  before failing.

### 12.2 "Worker won't acknowledge" — worker running but no ack

Check:
- Is the assignment identity valid? A mismatched `assignment_id` or
  `run_id` causes fail-closed rejection.
- Is the worker in the `PENDING` state? Only `PENDING` can acknowledge.
  If the worker was used for a previous assignment and not released,
  it may still be in a terminal state.
- Check `can_accept_assignments()` — only `RELEASED` workers can accept
  new work.

### 12.3 "Checkpoint rejected" — Core says wrong type or mismatched identity

Check:
- Does the checkpoint `assignment_id` and `run_id` match the
  assignment record?
- Is the checkpoint type valid for the current state? See the allowed
  transitions in `PoolWorkerRuntime._require_state()`.
- Is the checkpoint `type` in `CANONICAL_CHECKPOINT_TYPES`?

### 12.4 "Cleanup failed" — `PoolCleanupError` raised

Check:
- Which fields are missing? Run `CleanupEvidence.missing_fields()`.
- The four required fields are: `scrub_workspace`, `process_release`,
  `session_rotation`, `scratch_cleanup`.
- If any field is `False`, cleanup raises and the worker must be
  quarantined.
- In production, ensure your cleanup implementation sets all four
  fields to `True` before calling `cleanup()`.

### 12.5 "Worker stuck in terminal state" — no release or quarantine

Check:
- Is `quarantine_required()` returning `True`? If so, the worker
  reached a terminal state but cleanup was never called.
- Call `cleanup(evidence)` with complete evidence, then either
  `release()` or `quarantine()`.

### 12.6 "Den Web trace shows wrong state" — projection out of sync

Check:
- Did the assignment progress through the correct timeline? The
  `timeline_states` array in the evidence artifact records every
  state transition.
- The `state_label` is updated at each major transition. If a step
  was skipped (e.g. a runner checkpoint response was never sent),
  the trace will reflect the last stable state.

## 13. Reconciling stale active assignments

Use this recovery path only when the worker run and completion packet are already terminal, but the Core assignment/member projection is still active or busy.

### 13.1 Symptom

A stale active assignment usually has all of these signals:

- `get_worker_run_status(project_id, run_id, task_id)` reports a terminal worker run (`completed`, `failed`, or `blocked`) and a posted completion packet.
- `list_assignments(project_id, task_id, state=running|ack)` still shows the assignment as active.
- `list_pool_members(worker_identity=...)` still shows the pool member as `busy` even though the worker has finished.
- Cleanup evidence may already say `cleaned_up`, but the assignment has no terminal `completion`/`failure` checkpoint.

This was observed in #1789 for coder assignment #6 and reviewer assignment #8: completion packets existed, but the assignment rows remained active until a terminal checkpoint plus cleanup/release was applied.

### 13.2 Recovery sequence

Fail closed: verify the worker run really is terminal before applying these tools. Do not release an assignment for a still-running worker.

1. Reconcile the assignment to a terminal checkpoint:
   - Success: call `mcp_den_append_checkpoint(assignment_id=..., run_id=..., checkpoint_type="completion", payload=...)`.
   - Failure/blocked: call `mcp_den_append_checkpoint(..., checkpoint_type="failure", payload=...)` with the failure/block evidence.
   - Include `assignment_id`, `run_id`, `role`, completion packet/message id, branch/head/base when relevant, and a short evidence summary in the JSON payload.
2. Record cleanup evidence with `mcp_den_record_cleanup_evidence(assignment_id=..., evidence=...)`.
3. Release the assignment with `mcp_den_release_assignment(assignment_id=...)`.
4. Re-read `get_assignment(...)` and `list_pool_members(...)` to verify the assignment is terminal/released and the worker returned to `available` (or remained quarantined if policy/evidence requires it).

### 13.3 Why this should be rare after #1799

#1799 wires the spawned-Hermes orchestrator to perform the same sequence automatically after worker completion/failure publication:

```text
post_worker_completion_packet
  -> append assignment terminal checkpoint
  -> record cleanup evidence
  -> release assignment
```

If the stale-active pattern recurs after #1799, treat it as a regression in assignment lifecycle finalization and preserve the worker run id, assignment id, completion packet id, and pool member id in the task thread before retrying.

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

## 8. Pilot and test member lifecycle (retirement)

### 8.1 Current retired pilots

Three pilot pool members were created during the initial worker-pool rollout
and are now quarantined (retired):

| Worker identity | Type | Reason | Quarantined at |
|---|---|---|---|
| `den-hermes-runner` | #1739 preflight pilot | Failed closed: wake delivery stayed `recorded_pending_claim` | #1739 |
| `pool-coder-1739-preflight` | #1739 preflight pilot | Superseded by #1784 live provisioning; no real coder pool yet | #1782 |
| `den-mcp-runner` | #1728 live-smoke pilot | Old smoke-only member; not actionable | #1782 |

These members remain in the Core worker pool table with `status=quarantined`
so that assignment traces and audit evidence are preserved. They are **not**
returned as candidate workers by `lease_worker` — the lease system filters to
`status=available` only. They also do not appear in the Channels `#worker-pool`
lobby presence.

### 8.2 Creating a new pilot or test member

1. Register via `upsert_pool_member` with:
   - A descriptive `worker_identity` (e.g. `pool-coder-$(date +%s)-pilot`).
   - `status=available` initially.
   - Metadata documenting the pilot's purpose, task_id, and expected evidence.
2. Optionally register Channels lobby presence via `UpsertWorkerPoolLobbyPresence`
   so the member appears in Den Web.
3. Run the pilot. Collect assignment evidence (checkpoint IDs, completion
   packets, log paths).
4. **After the pilot is complete:**
   - **Successful and keeping**: Leave as `status=available`. Optionally
     rename to a permanent `worker_identity`.
   - **Deprecated or superseded (most test members)**: Quarantine via
     `quarantine_pool_member` with a descriptive reason. Do not delete
     — preserve evidence.
   - **Accidental or broken**: Quarantine with reason. Do not delete.

### 8.3 Retirement policy

- **Do not delete** pilot members from the worker pool table. Preserved
  quarantine records maintain traceability for assignment history, operational
  audits, and debug investigations.
- **Do not mark retired pilots available** without explicit re-approval.
  Re-activating a quarantined pilot requires a documented fresh preflight.
- **Test/proof members** follow the same lifecycle: create, use, quarantine
  or keep. No silent deletion.
- **The `#worker-pool` lobby** automatically excludes quarantined members.
- **Den Web UI** (introduced in #1781) treats quarantined members separately,
  rendering them in an "Archived / Legacy" section with reduced visual weight
  and a quarantine chip — never as candidate workers.

## 9. Green-path worker-pool workflow (exemplar: #1739)

This section documents the canonical end-to-end worker-pool assignment lifecycle.
The current default workflow is the #1789/#1800/#1799 green path captured in the
Den document `den-hermes-bridge/default-agent-workflow-green-path`; the #1739
handles below remain useful as an early concrete exemplar. Future Runner work
should treat this as the default Den-managed agent workflow, not a special-case
pilot.

### 9.1 Role selection

| Field | Exemplar value |
|---|---|
| Project | `den-hermes-bridge` |
| Task | #1739 (first real worker-pool pilot) |
| Role | `coder` |
| Profile | `spawned-coder` |
| Concrete identity | `pool-coder-1739-preflight` |

### 9.2 Assignment lifecycle (checkpoint sequence)

```
1. Core creates assignment/lease
   → assignment #2, run pilot-1739-20260530074809-fa245a3e

2. Gateway delivery + Channels wake
   → channel message #1419, gateway delivery #680
   → worker replies 1420/1421

3. assignment_ack checkpoint  #4  (runner response #3)
   → Worker acknowledges assignment; runner provides context.

4. interpretation checkpoint  #5  (runner response #3)
   → Worker publishes interpretation of task scope.

5. plan checkpoint           #6  (runner response #4)
   → Worker publishes its execution plan.

6. Wake for completion:
   → channel message #1425, gateway delivery #682
   → worker replies 1426/1428

7. completion checkpoint     #7
   → Final checkpoint. If clean, proceed to cleanup.

8. Cleanup evidence recorded:
   - scrub_workspace: passed/no-op
   - process_release: passed/no-op
   - session_rotation: passed/no-op
   - scratch_cleanup: passed/no-op

9. Core release or quarantine
   → Assigned status: completed
   → Completion packet #9316 (corrected from initial malformed #9315)
```

### 9.3 Key handles (from #1739)

| Handle | Value |
|---|---|
| Worker identity | `pool-coder-1739-preflight` |
| Work run | `pilot-1739-20260530074809-fa245a3e` |
| Session | `worker-1169c0018ed0a23f` |
| Assignment | `#2` (GET `/api/gateway/assignments/2/trace?projectId=den-hermes-bridge`) |
| Core availability | `available` with `gateway_available` evidence |
| Channel messages | `1419` (wake), `1420/1421` (ack reply), `1422` (plan wake), `1423/1424` (plan reply), `1425` (completion wake), `1426/1428` (completion reply) |
| Gateway delivery IDs | `680`, `681`, `682` |
| Channel membership marker | message #1431 (tagged `assignmentId=2`) |
| Checkpoint IDs | assignment_ack #4, interpretation #5, plan #6, completion #7 |
| Runner response IDs | #3 (for ack+interpretation), #4 (for plan) |
| Den Web trace | `GET /api/gateway/assignments/2/trace?projectId=den-hermes-bridge` |

### 9.4 Green-path success criteria

Before claiming completion, verify:

- [ ] Lease was granted and acknowledged via `assignment_ack` checkpoint.
- [ ] Interpretation and plan checkpoints posted and runner-acknowledged.
- [ ] Completion packet posted with correct `project_id`, `task_id`, `run_id`,
      `session_id`, `role`, `status`, `branch`, `base_commit`, `head_commit`,
      `tests_run`, `known_gaps`/`remaining_risks`, applicable finding IDs, and
      acceptance evidence.
- [ ] Runner-side validation passed before review/promotion.
- [ ] Review verdict is `looks_good`, no unresolved blocking findings remain, and
      the reviewed head exactly matches the current head being promoted.
- [ ] Cleanup evidence recorded (all four fields: scrub_workspace, process_release,
      session_rotation, scratch_cleanup).
- [ ] Core release or quarantine applied.
- [ ] Assignment trace accessible: `GET /api/gateway/assignments/<id>/trace?projectId=<project>`.
- [ ] Lobby presence reflects correct status (available back to idle, or quarantined).

### 9.5 Failure paths

| Failure mode | Exemplar | Recovery |
|---|---|---|
| Wake lifecycle ambiguity | Recorded direct-agent wake without claim evidence | Treat as `recorded_pending_claim` only; do not report the worker as started/running until claim/run evidence exists |
| No available worker | See #1785 no-capacity policy | Retry, wait, escalate to Patch/Planner |
| Ambiguous same-profile workers | Multiple ~reviewer~ bindings without concrete `pool_member_id` | Fail closed; require explicit `pool_member_id` in delivery |
| Unclaimed wake | `den-hermes-runner` pilot (#1739 preflight): both direct-agent messages stayed `recorded_pending_claim` | Quarantine the worker; investigate Gateway routing |
| Malformed completion packet | `pool-coder-1739-preflight` initially omitted `branch`/`head_commit` (#9315) | Runner corrects and reposts (#9316) |
| Cleanup uncertain/failure | Incomplete cleanup evidence → quarantine | Manual inspection via quarantine reason text |
| Quarantined/retired pilot members | `den-hermes-runner`, `pool-coder-1739-preflight`, `den-mcp-runner` | Lease system excludes `status=quarantined`; Den Web shows in archived section |

## 10. Worker-pool ownership boundaries

| Service | Owns | Key endpoints/files |
|---|---|---|
| **den-core** | Canonical pool member registry, lease lifecycle, no-capacity reads | `upsert_pool_member`, `lease_worker`, `quarantine_pool_member`, `list_pool_members`, `get_assignment`, `get_worker_pool_summary` |
| **den-channels** | `#worker-pool` lobby channel, membership, presence records, activity events | `GET /api/worker-pool/lobby/presence`, `UpsertWorkerPoolLobbyPresence`, `GET /api/channels?project=<project>` |
| **den-gateway** | Delivery routing, wake state, echo suppression, outage pause | Gateway delivery envelope, `agent_instance_bindings`, direct-agent message transport |
| **den-hermes-bridge** | Profile identity mapping, spawn-hermes runtime registry, role/profile/runtime glue | `spawned-hermes-runtimes.yaml`, `den_hermes/orchestrator.py`, `den_hermes/no_capacity_policy.py`, `scripts/provision_pool_workers.py` |
| **den-web** | Human-facing lobby, assignment traces, agents overview (including Worker Pool sub-tab) | Agents tab → Worker Pool sub-tab, `AssignmentTraceView`, `AgentsOverviewView` |
| **den-desktop** (future) | Desktop-focused worker-pool UX | TBD |

## 11. First-real-task checklist

When running the first real non-noop task through the worker-pool workflow:

### 11.1 Task selection

- [ ] Task is well-scoped: ≤3 files changed, deterministic output, clear acceptance criteria.
- [ ] All prerequisite tasks are done (dependencies resolved).
- [ ] Task has no external credential/network requirements (pool worker isolation is limited).
- [ ] Task does not require live-service endpoints that the pool worker cannot reach.

### 11.2 Role requirements

- [ ] At least one pool worker with matching role is available (`list_pool_members` shows `status=available` for the required profile).
- [ ] If a `scout` role is needed (codebase unfamiliar), a dedicated Scout worker must be available or a one-shot spawned-Hermes scraper must run first.
- [ ] If multiple roles are needed, they must be assigned sequentially (one lease at a time per project).

### 11.3 Pre-launch evidence

- [ ] Pool member identity, profile, and agent_instance_id recorded.
- [ ] Channels lobby presence confirmed (`GET /api/worker-pool/lobby/presence` includes the worker).
- [ ] Assignment trace baseline: no stale assignments for this worker.
- [ ] No-capacity fallback: if no worker is available, use #1785 policy (retry, wait, escalate — do not fake assignments).

### 11.4 Post-completion evidence

- [ ] Assignment trace returned `coreAvailability=available` with `gateway_available` evidence.
- [ ] All checkpoint IDs recorded (ack, interpretation, plan, completion).
- [ ] Completion packet verified: branch, head_commit, files_changed, tests_run, acceptance_evidence.
- [ ] Cleanup evidence recorded (all four fields).
- [ ] Worker returned to `available` in lobby (or quarantined with reason).
- [ ] Task thread posted with completion packet and trace links.

### 11.5 Cross-links

- Design/plan: #1685 (worker-pool implementation plan)
- First pilot: #1739 (first real worker-pool pilot)
- Orchestrator pool: #1762 (project-duration orchestrator pool assignments)
- Lobby presence: #1771 (visible #worker-pool home channel)
- Activity ordering: #1776 (fixed channel activity ordering)
- This umbrella: #1778 (worker-pool post-MVP operationalization)

## 14. Project-duration orchestrator pool leases

Use the pooled orchestrator lane when a project/workstream needs temporary coordination attention without creating a permanent project-specific Planner/Runner pair. The lane is intentionally distinct from bounded role-worker assignments:

- profile identity: `spawned-orchestrator`;
- concrete pool member: `pool-orchestrator-01`;
- Core role / lease kind: `project_orchestrator` / `pooled_orchestrator`;
- capability tags: `planning`, `task-shaping`, `den-coordination`, `worker-routing`, `checkpointing`.

### 14.1 Preflight before leasing

1. Confirm the Core project-duration lease APIs from den-core #1811 are deployed. Do **not** fake this with an ordinary task-worker assignment.
2. Read the target project/task/docs/messages and write the lease objective, scope, explicit non-scope, wake policy, checkpoint cadence, expiry, and release condition.
3. Verify three separate surfaces:
   - Channels membership in the target project/channel with intentional wake policy (`mentions_only` by default);
   - Gateway/Core binding for `spawned-orchestrator` / `pool-orchestrator-01`;
   - Core pool member status `available` for `worker_role=project_orchestrator`.
4. Verify the runtime registry resolves `project_orchestrator` to `spawned-orchestrator` and does not pass hidden `--yolo`, provider, model, or toolset overrides in persistent pool mode.

### 14.2 Lease / kickoff

1. Create a Core project-duration orchestrator lease, not an ordinary bounded `lease_worker` task assignment. Required fields should include `lease_kind=project_orchestrator`, `scope_type`, `project_id`, optional `channel_id`/`task_id`/workstream handle, objective, lease owner, duration/expiry, renewal/drain policy, pool member/profile/agent-instance ids, and wake/checkpoint cadence.
2. Join or activate `spawned-orchestrator` in the target project/channel for the lease window. Membership is presence; it is not itself the lease.
3. Post a kickoff checkpoint in the project channel/task thread with objective, scope, expiry, wake policy, source docs/tasks read, first handoff targets, and next checkpoint time.
4. If the kickoff cannot prove lease + binding + membership, mark the lease `degraded` or `blocked` and stop before routing real work.

### 14.3 During the lease

- Shape tasks, split/link dependencies, write durable handoffs, and route coder/reviewer/validator/drift/audit work through Den.
- Keep project facts in Den docs/tasks/messages/checkpoints; the profile keeps zero long-term/local memory.
- Post scheduled checkpoints with progress, open decisions, worker routes, and release/renewal posture.
- Ask Patch only for material product/architecture/safety decisions, not routine queue movement.
- Do not perform substantial implementation/review/validation directly as the orchestrator.
- Continue clear in-scope lease work without “can I continue?” prompts, but stop and ask a concrete question when the lease, task, workdir, project ownership, acceptance criteria, safety boundary, or authority path is genuinely unclear.
- Stay inside the assigned project/repo workdir and explicit Den artifact/context paths. Do not SSH to hosts, sweep tmux/systemd/journals/processes, alter services, chase fleet state, or treat Den task numbers as GitHub issues unless the lease explicitly grants infrastructure-diagnostic scope.
- If progress requires outside-workdir or host/service/fleet action, send a Den Channels/direct-agent request to `sysadmin` with task/lease id, evidence, requested action, and urgency instead of solving it ad hoc.

### 14.4 Renew / drain / release

- **Renew** only when the project still needs temporary coordination. Record the new expiry, objective delta, and checkpoint cadence in Core and the project channel.
- **Drain** when active handoffs are in flight but no new project-shaping work should start. Post the handoff list, unresolved blockers, and proposed release time.
- **Release** when the lease objective is satisfied or the project no longer needs the pool. Record cleanup/release evidence, deactivate/leave lease-specific channel membership as appropriate, and set the pool member back to available.
- **Quarantine/degrade** if profile auth/config drift, stale binding, or failed cleanup means the lane is not safely reusable.

### 14.5 Smoke checklist

A no-risk smoke should prove more than local process startup:

- `spawned-orchestrator` profile exists with `SOUL.md`, zero-memory posture, and `approvals.mode=off`.
- `hermes-gateway@spawned-orchestrator.service` is active and logs `✓ den_channels connected`.
- Channels membership and Core binding read back as active for the smoke project/channel.
- Pool member `pool-orchestrator-01` reads back with role `project_orchestrator` and expected capabilities.
- Direct Channels smoke receives a visible `gateway_delivery` reply from `spawned-orchestrator`.
- A real Core project-duration lease create/read/activate/release/cleanup cycle reads back distinctly from membership and binding.
- A leased direct handoff smoke produces a Den-visible kickoff/handoff response and does not take on implementation/review/validation work.

### 14.6 Live smoke evidence from #1812

Final #1812 verification after den-core #1811 deployed used the live Core facade at `http://192.168.1.10:18080/den-core-api`.

Source and runtime evidence:

- den-core live health after deploy: commit `c18dc36c4180`.
- Deploy backup path: `/data/services/den-core/app.previous.20260601T110052Z`.
- Focused server route tests on den-srv (192.168.1.10) before deploy: `dotnet test tests/DenMcp.Server.Tests/DenMcp.Server.Tests.csproj --filter OrchestratorLease --logger "console;verbosity=minimal"` → 4/4 passed.
- Project-orchestrator member readback: `pool-orchestrator-01`, `profile_identity=spawned-orchestrator`, `worker_role=project_orchestrator`, `status=available`, `agent_instance_id=den-k8plus:spawned-orchestrator:project_orchestrator:gateway`, channel `5`.

Lease lifecycle smoke:

- Lease run: `lease-smoke-1812-f40fc2d001bf`.
- Public lease id: `pool-orchestrator-01:den-hermes-bridge:d8252d4ff8a240d8a9460e0c309f3b3a`.
- API steps passed: create `201`, read by id `200`, read by public lease id `200`, residency while leased/active shows `residency_kind=orchestrator_lease`, transition to `active`, list active, transition to `released`, cleanup evidence recorded, final residency shows the pool member back as a `gateway_binding` with `state=available`.
- Local artifact: `/tmp/lease-smoke-1812-nc1bpjls/smoke.json`.

Leased handoff smoke:

- Lease run: `lease-handoff-smoke-1812-61d90504b3`.
- Public lease id: `pool-orchestrator-01:den-hermes-bridge:f28c53f67c5549f89f97bcc5d8cb857c`.
- Direct request message: Channels message `1823`.
- Final spawned-orchestrator reply: Channels message `1824`, gateway delivery `943`, containing `SPAWNED-ORCHESTRATOR-HANDOFF-OK` and explicitly stating no implementation work was taken on.
- Release/cleanup artifact: `/tmp/lease-handoff-1812-3-release.json`; final lease state `released` with cleanup evidence recorded.

Operational note: an earlier handoff attempt (`lease-handoff-smoke-1812-b28dc41540`, messages `1820`/`1821`) stalled while confirming Den state after a Core MCP reconnect; the lease was released with cleanup evidence and the spawned-orchestrator gateway was restarted before the successful final handoff smoke.

### 14.7 Lease-aware operator stop (from #1832)

An operator `/stop` sent to a leased pooled project_orchestrator profile now discovers active Core leases, drains/releases them, and reconciles stuck child assignments instead of returning "No active task to stop."

**Stop sequence** (invoked via `hermes orchestrator --stop` or Channels `/stop`):
1. Query Core for active `project_orchestrator` leases on the current project with `list_orchestrator_leases(project_id, include_terminal=false)`.
2. For each active lease: transition it through `draining` and then `released` with `transition_orchestrator_lease(lease_internal_id, new_state, evidence)` so cleanup evidence is attached and the pool member is freed.
3. Query Core for child assignments with `list_assignments(project_id, verbose=true)` and identify `launching`/`ack` zombies.
4. For each stuck child: append a zombie-cleanup failure checkpoint, record cleanup evidence, and release the assignment.
5. Return structured `OrchestratorStopResult` with status (`released`, `drained_with_errors`, `no_lease_active`, `reconciled_stuck`), lease count, released lease ids, cleaned assignment ids, and diagnostic summary.

**CLI invocation:**
```bash
python -m den_hermes.orchestrator --project-id <project> --task-id <task> --stop --stop-reason "Operator reclaiming pool member"
```

**Operator playbook:**
- Send `/stop` via Den Channels to the spawned-orchestrator profile lane.
- The profile invokes the lease-aware stop path; verify the structured output.
- Expected output includes `released`, `no_lease_active`, or `reconciled_stuck`.
- If `drained_with_errors`, inspect lease release errors and remedy manually.
- After stop, verify the Core pool summary shows the member as `available`.

### 14.8 Stuck child assignment cleanup green path (from #1832/#1833)

Child assignments that remain in `launching`/`ack` state with no bridge/process evidence (zombies) are cleaned up during orchestrator stop, but cleanup must be **claim-aware**:

- `DenWorkflowAdapter.list_active_child_assignments()` queries `list_assignments(project_id, verbose=true)` and identifies assignments with state/status `launching`, `ack`, or `acknowledged`.
- Before classifying a direct-agent launch as a zombie, inspect the Channels events around the direct wake message (query from `after_id=message_id-1`, not from channel start). If the target profile posts a `gateway_delivery` agent reply/progress event after the wake, the launch is claimed/in progress; do not fail or release that assignment merely because the Core worker-run record is still `launching` before the completion packet arrives.
- `DenChannelsAgentMessenger` reports this as `delivery_status="agent_reply_posted"` when no `deliveryRequestId` is available but the target agent replies through the gateway after the wake. Treat that as live claim evidence.
- Only when the assignment is still `ack`/`launching` **and** there is no direct wake claim/progress/completion evidence should `DenWorkflowAdapter.fail_child_assignment()` perform cleanup: mark the worker as failed with `failure_category=orchestrator_stop_zombie_cleanup`, append a failure checkpoint, record cleanup evidence, and release the assignment.
- Cleanup is best-effort per assignment — failures on one zombie do not block cleanup of others.

This preserves the manual cleanup green path for true zombies such as the GoblinBench #1752 child coder assignment #57, while avoiding premature failure of slow-but-claimed role workers like #61/#62/#63.

### 14.9 Diagnostic guardrails (from #1832)

Project-orchestrator deliveries now carry role-specific diagnostic guardrails in the wake envelope. The `_build_delivery_envelope()` function in `channels_bridge.py` injects bounded instructions:

**project_orchestrator guardrails:**
- Do NOT search GitHub/web for Den task numbers.
- Do NOT SSH to hosts or inspect tmux/systemd/journals/processes.
- Do NOT treat Den task IDs as GitHub issues.
- Limit diagnostics to: Den state (tasks, messages, docs, lease records), Core worker-pool bindings/assignments, Channels membership, runtime registry evidence.
- If infrastructure investigation is required, send a direct-agent request to `sysadmin`.
- Summarize findings without runaway command exploration.

**role-worker (coder/reviewer/validator/etc.) guardrails:**
- Work only within the assigned repo branch/worktree.
- Do NOT search GitHub/web for task numbers; use Den as the source of truth.
- Do NOT SSH to hosts or administer infrastructure.
- Run only local git/code/tests operations.

These guardrails prevent the diagnostic-routing issues observed in GoblinBench #1752 (controlled #1830 diagnostic wandered into GH/web/tmux/SSH/systemd sweeps).

### 14.10 OrchestratorStopResult schema

The `OrchestratorStopResult` dataclass (`orchestrator.py`) provides a deterministic, machine-readable stop outcome:

| Field | Type | Description |
|-------|------|-------------|
| `status` | str | `released`, `drained_with_errors`, `no_lease_active`, `reconciled_stuck`, `blocked`, `failed` |
| `run_id` | str | Stop operation run id |
| `lease_count` | int | Number of active leases found |
| `released_leases` | list[str] | Public lease ids successfully released |
| `reconciled_assignments` | int | Number of stuck child assignments cleaned up |
| `stuck_assignments_cleaned` | list[int] | Assignment ids cleaned up |
| `diagnostic` | str | Human-readable summary |
| `error` | str? | Aggregate error detail if any |

### 14.11 Cross-project worker-pool attribution (from #1834)

Pool workers are reachable via a shared control channel (channel 5, `den-hermes-bridge`). This is the **transport channel** — it is not the work project. Every cross-project wake must carry explicit target metadata to avoid collapsing attribution to the control channel's project.

**Attribution model:**

| Concept | Field | Example |
|---------|-------|---------|
| Transport/control channel | `channel_id` | 5 (den-hermes-bridge) |
| Target work project | `SourceProjectId` / `target_project_id` | `den-core` |
| Target task | `TargetTaskId` / `target_task_id` | 1820 |
| Target assignment | `AssignmentId` / `target_assignment_id` | 63 |

**Gateway message behavior (den-channels):**
- `POST /api/gateway/direct-agent-messages` accepts optional `sourceProjectId`, `targetTaskId`, and `assignmentId` fields.
- When provided, the channel message records `SourceProjectId: <caller's value>` instead of the channel's project.
- The response DTO echoes all three attribution fields.

**DenChannelsAgentMessenger behavior (den-hermes):**
- `send_agent_message()` accepts `target_project_id`, `target_task_id`, and `target_assignment_id`.
- These are passed through to the Gateway as `source_project_id`, `target_task_id`, and `assignment_id`.
- `AgentMessageResult` carries `target_project_id`, `target_task_id`, and `target_assignment_id` for code-level consumers.
- `project_id` on the result still reflects the channel's project (transport attribution).

**Backward compatibility:** Omitting target fields preserves existing behavior — the channel's project is used for `SourceProjectId`.

**Smoke defaults caveat:** `scripts/smoke_pool_worker_assignment.py` defaults `task_id=1784` / `project_id="den-hermes-bridge"`. These are **pilot provisioning defaults only**, not structural work-attribution truth. Cross-project callers must supply explicit `target_project_id`.

### 14.12 Pool-member metadata schema (from #1836)

Pool-member metadata is split into two layers to prevent historical provisioning defaults from being read as current work attribution.

**Operational / control topology** (top-level, preserved):
`worker_identity`, `profile_identity`, `worker_role`, `agent_instance_id`, `channel_id`, `session_id`, `status`, `capabilities`, `runtime_id`, `provider`, `model`, `registry_fingerprint`, `preflight`, `smoke`, `scout`, `lease_kind`, `memory_policy`, `core_dependency`, `timeout_seconds`.

**Provisioning provenance** (nested under `provisioning`, historical only):
`source` (e.g. `"provisioning_script"`, `"reviewed_provisioning_script"`), `repo` (e.g. `"den-hermes"`), `task_id` (e.g. `1784`), `commit`, `registry_fingerprint`.

**Normalization rules:**
- No bare `task_id`, `repo`, or `source` at the top level of pool member metadata.
- Historical values are moved under a `provisioning` object.
- `provision_pool_workers.py` emits `provisioning: {source, repo, registry_fingerprint}` in apply payloads.
- `smoke_pool_worker_assignment.py` nests smoke metadata under `provisioning` key.
- `Den MCP upsert_pool_member` consumers should read `provisioning` for audit/debug but resolve routing by `worker_role`/`profile`/`pool_member_id`.

**Before/after evidence** should be recorded in the task thread (#1836): before shows historical `task_id`/`repo`/`source`; after shows `provisioning` object.

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

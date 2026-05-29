# DenOps context-firewall assistant pilot (task #1726)

Status: pilot proposal. DenOps is not a registered canonical role in the
active central runtime registry (`spawned-hermes-runtimes.yaml`). This
document defines the proposed contract, narrow scope, receipt schema, and
smoke procedure for a context-firewall DenOps assistant that performs
mechanical Den bookkeeping and returns compact verified receipts.

## 1. Purpose

DenOps is a narrow-scope Hermes role for mechanical Den bookkeeping work
that does not require repo mutation, architecture decisions, or full
Planner/Runner context. DenOps assistants are useful for:

- Reading Den task/dependency/wake/document state and returning compact
  handles rather than raw MCP tool dumps.
- Verifying that a task dependency graph is in the expected state before
  passing control to a Runner or Planner.
- Producing structured receipt evidence that a bookkeeping operation
  completed (e.g. message posted, status checked, handle verified).
- Reducing Den MCP call noise in Planner/Runner context packets by
  pre-compacting task-thread state into a deterministic receipt.

The "context-firewall" property means: the DenOps assistant's output must
never leak raw Den API call dumps, tool arguments, or internal Hermes
state into receipts or summaries. Only compact structured evidence
(`handles`) passes the firewall.

## 2. Role contract

### 2.1 Scope (what DenOps does)

| Action | Allowed | Notes |
| --- | --- | --- |
| Read task thread messages via Den MCP | Yes | Must return compact handles, not raw dumps |
| Check task/dependency/wake/document status | Yes | Must return structured status handles |
| Post structured messages to Den task thread | Yes | Only message types listed in §2.3 |
| Verify message was posted (readback) | Yes | Readback evidence required in receipt |
| Produce compact verified receipt | Yes | Always required; receipt is the sole output |
| Check task status / list dependencies | Yes | Handle-based summary only |

### 2.2 Non-goals (what DenOps does NOT do)

| Action | Status | Rationale |
| --- | --- | --- |
| Modify repo files | Forbidden | No `git`, `write_file`, `patch`, or file edits |
| Modify Den Core schema | Forbidden | DenOps is Den *bookkeeping*, not Den *administration* |
| Close tasks silently | Forbidden | Task closure requires explicit instruction + readback evidence |
| Make architecture decisions of record | Forbidden | DenOps has no standing to commit architecture |
| Access credentials / env dumps | Forbidden | Profile names only; no `.env`, `auth.json`, or `DEN_*` exposure |
| Launch or manage other workers | Forbidden | DenOps is a single-role assistant, not an orchestrator |
| Expose raw MCP tool arguments in output | Forbidden | Handles are summaries; tool call details stay internal |
| Replace Runner review gate | Forbidden | Receipts inform Runner decisions but do not substitute them |
| Claim Den task completion | Forbidden | Completion is Runner-authoritative via tracked worker lifecycle |

### 2.3 Allowed Den MCP message types

DenOps may post only these message types to the Den task thread:

| `metadata.type` | Purpose | Receipt handles required? |
| --- | --- | --- |
| `denops_checkpoint` | Bookkeeping state checkpoint | Yes |
| `denops_verification_receipt` | Receipt evidence posted to thread | Yes |
| `blocked_needs_input` | Typed blocker with recovery guidance | Yes |

These types mirror the checkpoint protocol (task #1721) but add
`denops_checkpoint` and `denops_verification_receipt` as distinct
DenOps-specific message markers. Do not post `assignment_ack`,
`interpretation_checkpoint`, `plan_checkpoint`, or `checkpoint_response`
when operating as DenOps — those belong to coder/Runner lifecycle.

### 2.4 Receipt is mandatory

A DenOps run MUST produce a compact receipt (see §5) as its only output.
No prose-only responses, no raw tool dumps, no environment dumps. The
receipt is both the work artifact and the handoff payload.

If the DenOps assistant cannot produce a valid receipt (e.g. Den is
unreachable, MCP tools are missing), it MUST produce a `blocked` or
`failed` receipt with `blockers` explaining why, not a best-effort
prose summary.

## 3. Tool and profile boundaries

### 3.1 Recommended toolset

```
file, terminal (read-only: cat/ls/head only in workspace),
den_mcp (task read / message read / message post / status check only)
```

Do NOT include tools that write to filesystem, modify git, edit profiles,
access credentials, or invoke other workers. If the active Hermes profile
cannot be restricted to read-only tools, DenOps should not be launched.

### 3.2 Profile configuration (proposal)

The operator would configure a DenOps runtime entry in the central runtime
registry (`spawned-hermes-runtimes.yaml`) if/when DenOps graduates from
pilot to active role:

```yaml
runtime_defs:
  denops-pilot:
    role: denops
    hermes_binary: /usr/bin/hermes
    profile: den-denops-assistant
    provider: glm-5.1
    model: glm-5.1
    toolsets: ["file", "den_mcp"]
    timeout_seconds: 120
    preflight:
      prompt: |
        You are a DenOps context-firewall assistant.
        Reply with exactly: DENOPS_OK
      expected: DENOPS_OK
```

The `denops` role is NOT in the current `CANONICAL_WORKER_ROLES` set
(`coder`, `reviewer`, `validator`, `drift_checker`, `packet_auditor`).
This registry entry is a proposal only; adding it to the active registry
requires an operator decision after pilot review.

### 3.3 Active role constraint

PoolWorkerProfileGuide validation currently rejects roles outside
`CANONICAL_WORKER_ROLES`. A PoolWorkerProfileGuide for DenOps role
would fail validation today. This is intentional — the DenOps profile
support is proposal/pilot and MUST NOT silently pass validation until
the operator explicitly updates the canonical role set and registry.

## 4. Non-goals (expanded)

Beyond the action-level non-goals in §2.2, the following design
boundaries apply:

- **No receipt aggregation**: DenOps runs are single-shot. The assistant
  does not maintain long-lived state or aggregate receipts across runs.
- **No parallel operations**: DenOps performs sequential bookkeeping
  steps and reports one receipt per run.
- **No silent retry**: If an operation fails, the receipt captures the
  failure; DenOps does not retry silently or mask errors.
- **No context carryover**: Each DenOps run starts fresh with only the
  launch context packet. No memory tool, no session history.
- **No runner delegation**: DenOps cannot delegate to other workers.
  If bookkeeping requires further action, the receipt's
  `next_required_action` field instructs the Runner.

## 5. Receipt schema

Every DenOps run produces a JSON receipt document. The schema is defined
in `scripts/validate_denops_receipt.py` and applies deterministically
(pure JSON structure checks, no network I/O).

### 5.1 Required top-level fields

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `receipt_version` | string | yes | Schema version, currently `"1.0"` |
| `status` | string | yes | One of: `completed`, `partial`, `blocked`, `failed` |
| `summary` | string | yes | One-line human-readable summary |
| `handles` | object | yes | Compact structured evidence (see §5.2) |
| `blockers` | array | yes | List of blocker objects (empty if none) |
| `assumptions` | array | yes | Assumptions the receipt depends on |
| `next_required_action` | string | yes | What the Runner should do next |
| `verification` | object | yes | Readback evidence (see §5.3) |

### 5.2 `handles` object fields

All handle fields are optional per receipt. For `completed` and
`partial` receipts, at least one handle type MUST be present with
non-empty content. `blocked` and `failed` receipts MAY have empty handle
arrays when the blocker or failure object carries the actionable evidence.
Each non-empty handle type provides enough fields for the Runner to read
back the Den state without re-querying.

#### 5.2.1 `handles.tasks` (array)

Each task handle:

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `task_id` | integer | yes | Den task ID |
| `project_id` | string | yes | Den project identifier |
| `status` | string | yes | Task status as observed |
| `message_count` | integer | no | Count of messages in task thread |
| `latest_message_id` | integer | no | Most recent message ID |
| `dependencies` | array | no | List of dependency task IDs (integers) |

#### 5.2.2 `handles.messages` (array)

Each message handle:

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `message_id` | integer | yes | Den task-thread message ID |
| `type` | string | yes | `metadata.type` of the message |
| `summary` | string | yes | Compact summary of message content (≤200 chars) |
| `timestamp` | string | no | ISO 8601 or Den-observed timestamp |

#### 5.2.3 `handles.documents` (array)

Each document handle:

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `document_id` | string | yes | Den document reference or name |
| `project_id` | string | yes | Project the document belongs to |
| `summary` | string | yes | Compact summary |
| `status` | string | no | Document status if available |

#### 5.2.4 `handles.wake_events` (array)

Each wake event handle:

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `event_id` | string | yes | Wake event identifier |
| `channel_id` | integer | no | Den Channels channel ID |
| `summary` | string | yes | Compact summary |
| `status` | string | yes | `pending`, `delivered`, or `acknowledged` |

#### 5.2.5 `handles.delivery_requests` (array)

Each delivery request handle:

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `request_id` | string | yes | Delivery request identifier |
| `target_identity` | string | yes | Intended recipient |
| `summary` | string | yes | Compact summary |
| `status` | string | yes | `posted`, `pending`, `delivered`, or `failed` |

### 5.3 `verification` object fields

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `readback_messages_checked` | boolean | yes | Whether posted messages were read back |
| `task_thread_readback` | boolean | yes | Whether task thread state was read back |
| `handle_integrity` | boolean | yes | Whether handles were verified for required fields |
| `verification_method` | string | no | How verification was performed (e.g. `den_mcp_read_task`, `den_mcp_read_message`) |

### 5.4 Status values

| Status | Meaning |
| --- | --- |
| `completed` | All bookkeeping operations successful; receipt contains valid handles |
| `partial` | Some operations succeeded, some failed; receipt captures partial handles |
| `blocked` | DenOps cannot proceed; receipt has blockers array with recovery guidance |
| `failed` | Infrastructure failure, authentication failure, or invalid input |

### 5.5 Blocker object fields

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `blocker_summary` | string | yes | One-line blocker description |
| `blocker_category` | string | yes | One of: `needs_runner_decision`, `infrastructure`, `den_unreachable`, `tool_missing`, `authentication`, `unexpected_state` |
| `evidence_handles` | array | no | References to Den state that supports the blocker |
| `recovery_guidance` | string | yes | What the Runner should do to unblock |

### 5.6 Assumption object fields

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `assumption` | string | yes | Description of the assumption |
| `verified` | boolean | yes | Whether this assumption was verified from Den state |

### 5.7 Example completed receipt

```json
{
  "receipt_version": "1.0",
  "status": "completed",
  "summary": "Verified task #1726 state, observed interpretation checkpoint, no wake events pending.",
  "handles": {
    "tasks": [
      {
        "task_id": 1726,
        "project_id": "den-hermes-bridge",
        "status": "in_progress",
        "message_count": 3,
        "latest_message_id": 8650
      }
    ],
    "messages": [
      {
        "message_id": 8648,
        "type": "coder_context_packet",
        "summary": "DenOps context-firewall pilot launch packet",
        "timestamp": "2026-05-29T13:00:00Z"
      },
      {
        "message_id": 8650,
        "type": "denops_checkpoint",
        "summary": "DenOps bookkeeping checkpoint posted",
        "timestamp": "2026-05-29T13:01:00Z"
      }
    ],
    "wake_events": [],
    "delivery_requests": []
  },
  "blockers": [],
  "assumptions": [
    {
      "assumption": "Den task #1726 exists and is in 'in_progress' state",
      "verified": true
    },
    {
      "assumption": "No concurrent modifications to task thread during bookkeeping",
      "verified": false
    }
  ],
  "next_required_action": "Runner reviews receipt and proceeds with checkpoint protocol if approved.",
  "verification": {
    "readback_messages_checked": true,
    "task_thread_readback": true,
    "handle_integrity": true,
    "verification_method": "den_mcp_read_task + den_mcp_read_messages"
  }
}
```

### 5.8 Example blocked receipt

```json
{
  "receipt_version": "1.0",
  "status": "blocked",
  "summary": "Cannot verify task #1726 state: task not found or access denied.",
  "handles": {
    "tasks": [],
    "messages": []
  },
  "blockers": [
    {
      "blocker_summary": "Den task #1726 is not accessible or does not exist",
      "blocker_category": "unexpected_state",
      "evidence_handles": ["Den returned empty/null for task 1726 query"],
      "recovery_guidance": "Runner to verify task ID and project permissions, then relaunch DenOps."
    }
  ],
  "assumptions": [],
  "next_required_action": "Runner verifies task #1726 exists and relaunches DenOps.",
  "verification": {
    "readback_messages_checked": false,
    "task_thread_readback": false,
    "handle_integrity": false
  }
}
```

## 6. Smoke procedure

### 6.1 Deterministic fake smoke

Within this coder task, smoke is performed using a deterministic fake
receipt that validates against the schema without requiring live Den
MCP connectivity. The fake receipt produces handles that match the
expected contract shape.

Run the smoke:

```bash
# Validate the deterministic example receipts against the schema
python scripts/validate_denops_receipt.py \
  --receipt docs/examples/denops-receipt-completed.json

python scripts/validate_denops_receipt.py \
  --receipt docs/examples/denops-receipt-blocked.json
```

Both must exit 0 with "All validations passed."

### 6.2 Live-safe smoke (Runner after merge)

After this pilot document is merged, the Runner MAY perform a live-safe
smoke using an actual Den MCP profile with these steps:

1. Launch a DenOps Hermes subprocess with narrow toolset (`file, den_mcp`)
   and profile `den-denops-assistant` (or equivalent).
2. Instruct the DenOps assistant to:
   - Read Den task-thread messages for a known task (e.g. #1726 or a
     disposable smoke task).
   - Verify task state.
   - Post a `denops_checkpoint` message to the task thread with
     compact summary.
   - Read back the posted message to confirm delivery.
   - Produce a compact receipt.
3. Validate the receipt against `scripts/validate_denops_receipt.py`.
4. Verify that:
   - Receipt has `status: completed`.
   - `handles.tasks[0].task_id` matches the target task.
   - `handles.messages` contains at least one entry for the posted
     `denops_checkpoint`.
   - `verification.readback_messages_checked` is `true`.
   - No raw tool dumps, API keys, env vars, or profile content appear
     in the receipt.
5. Post lessons back to the den-core #1685 task thread.

### 6.3 Smoke lesson fields

After live smoke, capture and post these lesson fields:

| Field | Description | Example |
| --- | --- | --- |
| `lesson_id` | Deterministic or supplied handle | `denops-smoke-1726-20260529` |
| `lesson.source` | Smoke source identifier | `denops-context-firewall-pilot-smoke` |
| `lesson.verdict` | `passed`, `blocked`, or `failed` | `passed` |
| `lesson.timestamp` | ISO 8601 timestamp | `2026-05-29T14:00:00Z` |
| `lesson.run_id` | The smoke DenOps run ID | `t1726-denops-smoke-abc123` |
| `lesson.receipt_path` | Path to smoke receipt | `/tmp/den-hermes/t1726-denops-smoke/receipt.json` |
| `lesson.den_task_thread_message_id` | Den message ID if posted | `8650` |
| `lesson.notes` | Free-text observations | `All handle fields present and valid.` |

## 7. Validation

### 7.1 Receipt validation script

Run the receipt schema validator:

```bash
python scripts/validate_denops_receipt.py
```

Without arguments, validates the example receipts under
`docs/examples/`. With `--receipt <path>`, validates a specific file.
The validator is fully deterministic: no network I/O, no Den API calls,
pure JSON schema and structural checks.

### 7.2 Pytest coverage

```bash
python -m pytest tests/test_denops_receipts.py -v
```

Covers:
- Valid completed receipt passes.
- Valid blocked receipt passes.
- Missing required field fails closed.
- Wrong status value fails.
- Empty handles (no tasks, no messages, etc.) fails.
- Malformed JSON fails.
- Wrong types (string instead of integer for task_id) fail.

### 7.3 Git hygiene

```bash
git diff --check
```

No whitespace errors, no trailing whitespace, no merge conflict markers.

## 8. Lessons for den-core #1685

This pilot produces the following refinements to the worker-pool
assignment and checkpoint contracts described in
`den-core/worker-pools-implementation-plan-1685`:

### 8.1 Receipt-only handoffs

DenOps demonstrates that a spawned worker can return a compact,
deterministically validated receipt instead of a free-form prose
artifact. The receipt schema enforces structured handles, blocking
conditions, and verification evidence. **Recommendation**: generalise
receipt validation to the checkpoint protocol so that any worker type
(`coder`, `reviewer`, `validator`) can optionally use a compact receipt
when the task scope is narrow enough.

### 8.2 Context-firewall pattern

The "context-firewall" pattern prevents raw MCP tool call dumps from
leaking into Planner/Runner context packets. A DenOps receipt
abstracts Den state into handles with enough detail for audit but no
implementation surface. **Recommendation**: define a general
`compact_handles` field for coder/orchestrator context packets that
pre-verified Den state can populate, reducing the volume of
task-thread reads downstream workers must perform.

### 8.3 Role cardinality vs scope

DenOps has a narrower scope than existing canonical roles but does not
introduce a new role at the runtime registry level. This suggests that
the dimension of variation is not just *role* but *operation intent*
within a role. **Recommendation**: allow PoolWorkerProfileGuide to
constrain toolsets and allowed actions per runtime entry without
requiring a new canonical role; the `denops` runtime could reuse the
`coder` role with a narrower toolset and a profile that enforces the
context-firewall receipt contract.

### 8.4 Blocked receipt as check-in point

The blocked receipt schema (with `blocker_category`, `evidence_handles`,
and `recovery_guidance`) mirrors the `blocked_needs_input` checkpoint
type from the checkpoint protocol. **Recommendation**: align the
blocked receipt schema fields with `blocked_needs_input` content
fields so that a DenOps blocked receipt can be directly posted as a
task-thread checkpoint without field translation.

### 8.5 Verification evidence as first-class field

The `verification` object in the receipt schema makes readback evidence
mandatory: every claimed operation must have a corresponding
verification check. **Recommendation**: add an optional
`verification_evidence` field to `CompletionPacket` in the pool
runtime so that any worker can embed readback proof in its completion
artifact.

## 9. Relationship to existing contracts

### 9.1 PoolWorkerRuntime and PoolWorkerProfileGuide

The existing `PoolWorkerRuntime` state machine and `PoolWorkerProfileGuide`
validate role membership against `CANONICAL_WORKER_ROLES`. The `denops`
role is NOT in that set and MUST NOT be silently accepted. This pilot
document proposes the role and its registry entry, but the operator must
explicitly update the canonical role set and registry before DenOps can
be used in production.

### 9.2 Checkpoint protocol (task #1721)

The DenOps receipt schema complements the checkpoint protocol:
- Checkpoints are intermediate coordination messages (posted to Den
  task-thread during a run).
- Receipts are the terminal artifact of a DenOps run.
- A DenOps run may post a `denops_checkpoint` checkpoint message as a
  side effect, but the receipt is the primary output.

### 9.3 Pool-worker runtime contract (task #1373)

The existing runtime contract defines lifecycle states, artifact
requirements, and registration-before-launch invariants. DenOps
operates within this lifecycle: it registers as a worker run, produces
a deterministic receipt artifact, and completes with an
implementation packet. The difference is that DenOps's artifact is a
receipt rather than a code diff or review findings.

### 9.4 Agent-worker substrate policy

Per `_global/agent-worker-substrate-policy`, substantial work must use
spawned-Hermes tracked roles from the current registry. DenOps is
documented as a proposal/pilot. If an operator wishes to use DenOps
before the canonical role update, they must either:
- Add `denops` to `CANONICAL_WORKER_ROLES` and configure a registry
  entry; OR
- Reuse an existing role (e.g. `coder`) with a narrow toolset and
  enforce the receipt contract via the profile guide.

This document recommends the latter for pilot safety.

## Appendix A: Receipt schema field summary

```
receipt_version (string, required): "1.0"
status (string, required): completed | partial | blocked | failed
summary (string, required): one-line summary
handles (object, required):
  tasks (array, optional):
    - task_id (integer, required)
    - project_id (string, required)
    - status (string, required)
    - message_count (integer, optional)
    - latest_message_id (integer, optional)
    - dependencies (array of integers, optional)
  messages (array, optional):
    - message_id (integer, required)
    - type (string, required)
    - summary (string, required, <=200 chars)
    - timestamp (string, optional)
  documents (array, optional):
    - document_id (string, required)
    - project_id (string, required)
    - summary (string, required)
    - status (string, optional)
  wake_events (array, optional):
    - event_id (string, required)
    - channel_id (integer, optional)
    - summary (string, required)
    - status (string, required)
  delivery_requests (array, optional):
    - request_id (string, required)
    - target_identity (string, required)
    - summary (string, required)
    - status (string, required)
blockers (array, required): [...] (empty if none)
  - blocker_summary (string, required)
  - blocker_category (string, required)
  - evidence_handles (array, optional)
  - recovery_guidance (string, required)
assumptions (array, required): [...] (empty if none)
  - assumption (string, required)
  - verified (boolean, required)
next_required_action (string, required)
verification (object, required):
  readback_messages_checked (boolean, required)
  task_thread_readback (boolean, required)
  handle_integrity (boolean, required)
  verification_method (string, optional)
```

## Appendix B: Mandatory receipt checklist

Before posting a DenOps receipt:

- [ ] All required top-level fields present
- [ ] `status` is one of: completed, partial, blocked, failed
- [ ] At least one handle array has non-empty content
- [ ] Each handle object has all required fields
- [ ] `blockers` is empty array when status is completed/partial
- [ ] `blockers` is non-empty array when status is blocked/failed
- [ ] Assumptions have both `assumption` and `verified` fields
- [ ] `verification` has all three boolean fields
- [ ] No raw tool dumps, environment variables, or secrets visible
- [ ] `summary` is ≤200 characters and does not contain raw tool output

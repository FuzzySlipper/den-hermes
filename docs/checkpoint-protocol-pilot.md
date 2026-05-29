# Runner-supervised checkpoint protocol pilot (task #1721)

Status: pilot guidance, templates, and smoke infrastructure.
Core schema: no additions. Kanban/parallel board: not introduced.

## 1. Purpose

The existing spawned-Hermes orchestrator workflow already supports
Runner-supervised checkpoints as described in
`references/scout-and-checkpoint-expanded-delegation.md`. This document
formalizes the checkpoint packet templates, mandatory/optional rules,
worker handoff wording, and pilot smoke handles so the protocol can be
used without relying on shared-skill prose alone.

Checkpoints are a control gate, not free-form chat. The goal is early
drift detection during substantial or architecture-sensitive work,
before broad implementation commits to the wrong approach.

## 2. Checkpoint packet type definitions

All checkpoint messages are Den task-thread messages with structured
`metadata.type` values. They are **not** Den worker completion packets;
they are intermediate coordination messages that live in the task-thread
conversation alongside context packets, review findings, and lifecycle
notes.

The Runner reads checkpoint messages from the task thread, approves or
corrects them, and then either continues or blocks the workflow.

### 2.1 assignment_ack

Sent by a coder (or any spawned worker) immediately after launch, before
any implementation work. Confirms identity and task understanding.

```json
{
  "metadata": {
    "type": "assignment_ack",
    "project_id": "den-hermes-bridge",
    "task_id": 1721,
    "run_id": "t1721-coder-20260529...",
    "role": "coder"
  },
  "content": {
    "acknowledged_task": "Implement checkpoint protocol pilot",
    "interpretation_summary": "Draft checkpoint packet templates, mandatory rules, runner guidance, concrete examples, validation script, and pilot smoke section. No Core schema changes.",
    "uncertainties": ["Whether Runner approves the six templates as-is or wants adjustment before draft"],
    "non_goals": ["No Hermes Kanban introduction", "No Core schema additions", "No changes to den-core"],
    "repo_path": "/tmp/den-worktrees/den-hermes-1721"
  }
}
```

Required fields: `metadata.type`, `metadata.task_id`, `metadata.run_id`,
`metadata.role`, `content.acknowledged_task`,
`content.interpretation_summary`.

Optional fields: `content.uncertainties`, `content.non_goals`,
`content.repo_path`, `content.scout_evidence`.

### 2.2 interpretation_checkpoint

Sent by a coder after `assignment_ack`, detailing the full interpretation
of the task before any implementation. Includes acceptance criteria,
non-goals, and risks.

```json
{
  "metadata": {
    "type": "interpretation_checkpoint",
    "project_id": "den-hermes-bridge",
    "task_id": 1721,
    "run_id": "t1721-coder-20260529...",
    "role": "coder"
  },
  "content": {
    "accepted_criteria": [
      "Six packet templates defined with metadata.type values",
      "Mandatory/optional rules documented",
      "Runner/operator guidance updated",
      "Concrete examples showing Den task-thread as authoritative state",
      "Den Channels direct-agent messages shown as wake-only surface",
      "Validation script under scripts/ or tests/",
      "Pilot smoke section with lesson fields"
    ],
    "non_goals": [
      "No Core schema additions",
      "No Hermes Kanban",
      "No parallel board",
      "No changes to den-mcp or den-core"
    ],
    "risks": [
      "Checkpoint overhead may be perceived as friction for small tasks — mitigated by mandatory/optional distinction"
    ],
    "scout_evidence": {
      "existing_paths": [
        "docs/spawned-hermes-tracked-worker-runtime-contract.md",
        "docs/spawned-hermes-orchestrator-rollout-1402.md",
        "docs/scout-and-checkpoint-expanded-delegation.md (in shared skill references)"
      ],
      "duplicate_risks": [
        "Do not duplicate the scout+checkpoint reference — extend its concepts into a stand-alone pilot doc"
      ]
    }
  }
}
```

Required fields: `metadata.type`, `content.accepted_criteria`,
`content.non_goals`.

Optional fields: `content.risks`, `content.scout_evidence`.

### 2.3 plan_checkpoint

Sent after Runner approves the interpretation checkpoint. Names exact
files/paths to touch, approach, and validation plan.

```json
{
  "metadata": {
    "type": "plan_checkpoint",
    "project_id": "den-hermes-bridge",
    "task_id": 1721,
    "run_id": "t1721-coder-20260529...",
    "role": "coder"
  },
  "content": {
    "files_to_touch": [
      "docs/checkpoint-protocol-pilot.md (new)",
      "scripts/validate_checkpoint_protocol.py (new)",
      "docs/spawned-hermes-tracked-worker-runtime-contract.md (update — add mandatory checkpoint rule section)",
      "docs/tracked-spawned-hermes-worker-rollout-guidance.md (update — add checkpoint guidance)",
      "den_hermes/agent_message.py (no changes — referenced as wake-channel example only)"
    ],
    "approach": "Write standalone doc with templates, rules, examples, and smoke section. Add validation script following existing validate_doc_*.py patterns. Update existing contracts only where they reference checkpoint/mandatory-gate behavior.",
    "validation_plan": "python scripts/validate_checkpoint_protocol.py — passes all term/section/template checks",
    "risk_flags": [
      "Existing shared skill SKILL.md has checkpoint language that should not be duplicated or contradicted — reference it, do not replace it"
    ],
    "deferred_non_goals": [
      "No live Den message smoke — Runner may post smoke messages after doc is ready"
    ]
  }
}
```

Required fields: `metadata.type`, `content.files_to_touch`,
`content.approach`, `content.validation_plan`.

Optional fields: `content.risk_flags`, `content.deferred_non_goals`,
`content.vertical_slice_description`.

### 2.4 checkpoint_response

Sent by the Runner (or delegated approval authority) to approve,
approve-with-correction, request-changes, or block a checkpoint.

```json
{
  "metadata": {
    "type": "checkpoint_response",
    "project_id": "den-hermes-bridge",
    "task_id": 1721,
    "responds_to_checkpoint_type": "interpretation_checkpoint",
    "responds_to_run_id": "t1721-coder-20260529..."
  },
  "content": {
    "verdict": "approved" | "approved_with_correction" | "changes_requested" | "blocked",
    "correction": "Optional correction or instruction when verdict is not plain approved",
    "notes": "Optional context for the worker"
  }
}
```

Required fields: `metadata.type`, `metadata.responds_to_checkpoint_type`,
`metadata.responds_to_run_id`, `content.verdict`.

When verdict is `approved_with_correction` or `changes_requested`,
`content.correction` is required.

### 2.5 partial_result_checkpoint

Sent during implementation of broad/risky work. Implements a vertical
slice and asks Runner to inspect before expansion.

```json
{
  "metadata": {
    "type": "partial_result_checkpoint",
    "project_id": "den-hermes-bridge",
    "task_id": 1721,
    "run_id": "t1721-coder-20260529...",
    "role": "coder"
  },
  "content": {
    "vertical_slice_paths": ["docs/checkpoint-protocol-pilot.md"],
    "status": "first_section_draft",
    "diff_summary": "Full doc drafted, not yet validated or reviewed",
    "open_issues": [
      "Runner to confirm smoke lesson fields match expectations"
    ]
  }
}
```

Required fields: `metadata.type`, `content.vertical_slice_paths`,
`content.status`.

Optional fields: `content.diff_summary`, `content.open_issues`,
`content.tests_run`.

### 2.6 blocked_needs_input

Typed blocker with one actionable summary sentence and deeper evidence
handles. Can be sent by a coder or any worker that cannot proceed.

```json
{
  "metadata": {
    "type": "blocked_needs_input",
    "project_id": "den-hermes-bridge",
    "task_id": 1721,
    "run_id": "t1721-coder-20260529...",
    "role": "coder"
  },
  "content": {
    "blocker_summary": "Cannot determine which doc template structure Runner expects without explicit example reference",
    "blocker_category": "needs_runner_decision" | "needs_planner_context" | "infrastructure" | "external_dependency",
    "evidence_handles": [
      "Docs inspected: docs/, shared-skill references/",
      "Den packet message id: 8497"
    ],
    "recovery_guidance": "Runner to post checkpoint_response with verdict=approved_with_correction or content.correction describing preferred template format"
  }
}
```

Required fields: `metadata.type`, `content.blocker_summary`,
`content.blocker_category`, `content.recovery_guidance`.

Optional fields: `content.evidence_handles`, `content.triage_notes`.

## 3. Mandatory vs optional checkpoint rules

### 3.1 Mandatory checkpoints

Checkpoints are mandatory (workers must stop after producing a
checkpoint and wait for Runner `checkpoint_response` before proceeding)
when the task involves any of:

- **Architecture**: changing service boundaries, adding/modifying/removing
  a route, adapter, delivery path, state machine, or service lifecycle.
- **Schema**: adding/modifying/removing Den Core schema, database schema,
  MCP tool schemas, config schema, or serialization format.
- **Wake**: changing how agents are woken, messaged, dispatched, or how
  delivery queues work.
- **Memory**: changing how agent memory is stored, retrieved, scoped, or
  deleted.
- **Worker orchestration**: changing worker registration, launch,
  lifecycle, completion, artifact contract, or role mapping.
- **Broad implementation** cannot proceed from prose-only checkpoints on
  mandatory-gated tasks: at minimum a `interpretation_checkpoint` and
  `plan_checkpoint` with Runner acceptance must be posted to the Den
  task-thread before the coder modifies any source file beyond docs.

Workers must stop after posting an `interpretation_checkpoint` or
`plan_checkpoint` on mandatory-gated tasks until the Runner replies with
a `checkpoint_response`. If no response arrives within a reasonable
timeout, the worker should post a `blocked_needs_input` with
`blocker_category: needs_runner_decision`.

### 3.2 Optional checkpoints

Checkpoints are optional but recommended when:

- The task is medium-complexity but does not touch the mandatory categories.
- The Runner is unfamiliar with the code area.
- Previous attempts at similar tasks drifted.

### 3.3 When to skip checkpoints

Checkpoints should be skipped for:

- Documentation-only edits and file management.
- IT/ops-only tasks (config, deployment, monitoring).
- Pure live-smoke / test execution.
- Emergency repair.
- Tiny known-path fixes where the Runner already knows the exact file/function.

## 4. Runner/operator guidance

### 4.1 Worker handoff wording

When the Runner initiates a coder (or other spawned worker) for
mandatory-gated work, the context packet should include text like:

> This task touches [architecture/schema/wake/memory/worker-orchestration].
> After interpreting the task, post an `interpretation_checkpoint` to the
> Den task-thread with `metadata.type: interpretation_checkpoint` before
> any implementation. Wait for my `checkpoint_response` before editing
> source files. If I do not respond within a reasonable time, post a
> `blocked_needs_input` with `blocker_category: needs_runner_decision`.

For plan-level checkpoint:

> After interpretation is approved, post a `plan_checkpoint` naming exact
> files to touch and your approach. Wait for approval before implementing.

For partial-result checkpoints on broad changes:

> For changes touching more than [3|5] files or crossing service boundaries,
> implement a vertical slice first and post a `partial_result_checkpoint`
> before expanding.

### 4.2 Worker must stop

The core operational rule for mandatory-gated tasks:

> Workers MUST NOT proceed with source-file edits after posting a mandatory
> checkpoint until the Runner posts a `checkpoint_response` with verdict
> `approved` or `approved_with_correction`. If the verdict is
> `changes_requested` or `blocked`, the worker must revise its checkpoint
> and repost. Workers that proceed without Runner approval on a
> mandatory-gated task are in drift and their output must be rejected or
> re-reviewed.

### 4.3 Runner response discipline

- Runner should respond to mandatory checkpoints before launching other
  workers or starting other tasks.
- `checkpoint_response` with `approved_with_correction` should include
  precise correction instructions, not vague encouragement.
- If Runner cannot review promptly, post a `checkpoint_response` with
  `verdict: blocked` and `correction: "Awaiting planner context; hold."`
  rather than leaving the worker hanging.

### 4.4 Resuming after checkpoint approval

When the Runner approves a plan checkpoint with `approved` or
`approved_with_correction`, the worker may proceed with implementation
as described in the approved plan. If approved_with_correction, the
worker must incorporate the correction before proceeding.

## 5. Concrete examples: Den task-thread vs Den Channels wake

### 5.1 Den task-thread metadata as authoritative state

Checkpoints, context packets, review findings, completion packets, and
all durable workflow state live in the **Den task-thread**. Den owns the
authoritative record.

Example — Runner reads an `interpretation_checkpoint` from the task
thread, posts a `checkpoint_response`, and the coder proceeds:

```
Task-thread message #8497 (context packet): Runner → Coder
  metadata.type: coder_context_packet
  content: "Implement checkpoint protocol pilot..."

Task-thread message #8500 (checkpoint): Coder → Runner
  metadata.type: interpretation_checkpoint
  content: { accepted_criteria: [...], non_goals: [...], ... }

Task-thread message #8501 (response): Runner → Coder
  metadata.type: checkpoint_response
  content: { verdict: "approved_with_correction",
             correction: "Add explicit 'no Core schema' language..." }

Task-thread message #8502 (revised checkpoint): Coder → Runner
  metadata.type: interpretation_checkpoint
  content: { accepted_criteria: [... revised ...], ... }

Task-thread message #8503 (response): Runner → Coder
  metadata.type: checkpoint_response
  content: { verdict: "approved" }

→ Coder proceeds to implementation.
```

The entire decision trail is recorded in the task-thread. Any agent or
human can replay the chain to understand why implementation took the
shape it did.

### 5.2 Den Channels direct-agent messages as wake surface only

Den Channels `send_direct_agent_message` is the **wake surface** — used
to alert the Runner that a checkpoint awaits review, not to carry
checkpoint state.

Example — coder posts a checkpoint, then wakes the Runner:

```
1. Coder posts `interpretation_checkpoint` to Den task-thread (message #8500).
2. Coder (or orchestrator bridge) calls
   `den_channels_send_direct_agent_message(
     channel_id=5,      # project-den-hermes-bridge default channel
     member_identity="den-hermes-runner",
     body="Checkpoint awaiting review: interpretation_checkpoint at
           task-thread message #8500 for task #1721."
   )`
3. Runner receives wake event via Gateway, reads message #8500 from
   the task thread, posts a `checkpoint_response`.
4. Runner's response may optionally wake the coder in turn:
   `den_channels_send_direct_agent_message(
     channel_id=5,
     member_identity="den-hermes-runner",  # or the coder's profile identity
     body="Checkpoint approved with correction — see message #8501."
   )`
```

Key invariant: the Den task-thread holds the authoritative state; the
Den Channels direct-agent message is only a **wake notification**.
Never put checkpoint content or structured data in the direct-agent
message body; put it in the task-thread and reference it by message
id.

Do not use `send_agent_stream_message` (agent-stream writes) as the
checkpoint wake mechanism. Use Den Channels direct-agent messages
as described in the green-path wrapper (`agent_message.py`) and
documented in `docs/agent-message-wake-green-path-1624.md`.

## 6. Relationship to existing contracts

### 6.1 Scout and checkpoint expanded delegation reference

The existing `scout-and-checkpoint-expanded-delegation.md` reference
(under the spawned-hermes-orchestrator shared skill) introduced
checkpoints as a concept. This pilot document:

- Formalizes the packet templates with required/optional fields.
- Adds mandatory/optional rules with explicit categories.
- Adds worker handoff wording.
- Adds concrete Den task-thread vs Den Channels wake examples.
- Adds pilot smoke infrastructure.

Do not delete or replace the shared-skill reference. This pilot doc
extends it with actionable templates and validation.

### 6.2 Tracked spawned-Hermes worker runtime contract

The existing `docs/spawned-hermes-tracked-worker-runtime-contract.md`
defines worker lifecycle states (including `blocked` and `needs_input`),
artifact requirements, and the registration-before-launch invariant.
Checkpoints are an intermediate coordination layer between worker launch
and artifact completion. They do not replace the completion/review
packet lifecycle.

### 6.3 Agent message green-path

The existing `docs/agent-message-wake-green-path-1624.md` defines the
standard `DenChannelsAgentMessenger` wrapper. Checkpoints use this same
wrapper for wake — they do not introduce a new wake mechanism.

## 7. Pilot smoke: lesson fields and expected handles

### 7.1 Smoke structure

After this document and its validation script are merged, the parent
Runner may (if desired) post actual Den checkpoint messages to the task
thread for live verification. The fields below define what the smoke
should capture.

**interpretation_checkpoint smoke message:**

Expected Den task-thread message with:
- `metadata.type: interpretation_checkpoint`
- `metadata.task_id: 1721`
- `metadata.project_id: "den-hermes-bridge"`
- `metadata.run_id: <smoke-run-id>`
- `metadata.role: "coder"`
- `content.accepted_criteria` — non-empty array
- `content.non_goals` — non-empty array, includes "no Core schema" and "no Kanban"

Expected lesson fields:
- `lesson_id`: a deterministic or supplied handle for correlating smoke results.
- `lesson.source`: "checkpoint-protocol-pilot-smoke"
- `lesson.phase`: "interpretation_checkpoint"
- `lesson.verdict`: "posted" | "accepted" | "rejected"
- `lesson.timestamp`: ISO 8601 timestamp
- `lesson.message_id`: the Den task-thread message id
- `lesson.notes`: free-text observations

**plan_checkpoint smoke message:**

Expected Den task-thread message with:
- `metadata.type: plan_checkpoint`
- `metadata.task_id: 1721`
- `metadata.project_id: "den-hermes-bridge"`
- `metadata.run_id: <smoke-run-id>`
- `metadata.role: "coder"`
- `content.files_to_touch` — non-empty array
- `content.approach` — non-empty string
- `content.validation_plan` — non-empty string

Expected lesson fields:
- `lesson_id`: deterministic or supplied handle
- `lesson.source`: "checkpoint-protocol-pilot-smoke"
- `lesson.phase`: "plan_checkpoint"
- `lesson.verdict`: "posted" | "accepted" | "rejected"
- `lesson.timestamp`: ISO 8601 timestamp
- `lesson.message_id`: Den task-thread message id
- `lesson.notes`: free-text observations

**checkpoint_response smoke message:**

Expected Den task-thread message with:
- `metadata.type: checkpoint_response`
- `metadata.responds_to_checkpoint_type: "interpretation_checkpoint"` or `"plan_checkpoint"`
- `metadata.responds_to_run_id: <smoke-run-id>`
- `content.verdict` — one of approved/approved_with_correction/changes_requested/blocked

Expected lesson fields:
- `lesson_id`: deterministic or supplied handle
- `lesson.source`: "checkpoint-protocol-pilot-smoke"
- `lesson.phase`: "checkpoint_response"
- `lesson.verdict`: "posted" | "accepted" | "rejected"
- `lesson.responds_to_type`: the checkpoint type being responded to
- `lesson.timestamp`: ISO 8601 timestamp
- `lesson.message_id`: Den task-thread message id
- `lesson.notes`: free-text observations

### 7.2 Smoke handles template

The Runner supplies these handles when posting live smoke messages:

| Handle | Description | Example |
| --- | --- | --- |
| `smoke_run_id` | Deterministic run id for the smoke | `t1721-smoke-20260529...` |
| `interpretation_checkpoint_message_id` | Den message id if posted | `#8510` (placeholder) |
| `plan_checkpoint_message_id` | Den message id if posted | `#8511` (placeholder) |
| `checkpoint_response_message_id` | Den message id if posted | `#8512` (placeholder) |
| `lesson_artifact_path` | Local path for smoke capture | `/tmp/den-hermes/t1721-smoke/lessons.json` |

Task #1721 pilot smoke handles produced during implementation:

| Handle | Value |
| --- | --- |
| `smoke_run_id` | `checkpoint-smoke-1721-20260529` |
| `interpretation_checkpoint_message_id` | `#8500` |
| `plan_checkpoint_message_id` | `#8501` |
| `checkpoint_response_message_id` | `#8502` |
| `checkpoint_response_verdict` | `approved_with_correction` |

### 7.3 Smoke verification

After smoke messages are posted, verify:

- Each message is visible in the Den task-thread with correct `metadata.type`.
- `checkpoint_response` correctly references `responds_to_checkpoint_type` and `responds_to_run_id`.
- No checkpoint content was duplicated in Den Channels direct-agent message bodies.
- Lesson fields are populated with real Den message ids and timestamps.

## 8. Validation

Run the validation script after any edits to this document:

```bash
python scripts/validate_checkpoint_protocol.py
```

The script checks required sections, required terms, and required
template-structure markers. All checks must pass before committing.

## Appendix A: Summary of metadata.type values

| metadata.type | Sender | Purpose | Expected before |
|---|---|---|---|
| `assignment_ack` | Worker | Confirm identity and task understanding | Implementation |
| `interpretation_checkpoint` | Worker | State task interpretation, criteria, non-goals | Implementation (mandatory) |
| `plan_checkpoint` | Worker | Name files, approach, validation plan | Implementation (mandatory) |
| `checkpoint_response` | Runner | Approve/correct/block checkpoint | Worker proceeds |
| `partial_result_checkpoint` | Worker | Vertical slice for risky/broad changes | Expansion |
| `blocked_needs_input` | Worker | Typed blocker with recovery guidance | Runner unblocks |

## Appendix B: Quick reference — mandatory gate checklist

For the Runner before launching a coder on mandatory-gated work:

- [ ] Identify which mandatory categories apply (architecture/schema/wake/memory/worker-orch).
- [ ] Include "stop after mandatory checkpoints" wording in coder context packet.
- [ ] Prepare to respond to `interpretation_checkpoint` and `plan_checkpoint` promptly.
- [ ] If live smoke after merge: post a real `checkpoint_response` in the task thread.
- [ ] Do not treat checkpoint-missing artifacts as valid completion on mandatory tasks.

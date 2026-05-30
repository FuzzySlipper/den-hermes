# Spawned-Hermes Scout role design

Task: `den-hermes-bridge` #1691
Related: `den-hermes-bridge` #1779 (worker role catalog)
Parent: `den-core` #1778 worker-pool operationalization
Status: Design specification for Scout role

## 1. Purpose

Define the spawned-Hermes Scout role — a pre-coder codebase discovery
worker that surveys repository structure, existing patterns, and risk
areas before the coder starts implementation. The Scout output feeds
into the coder context packet, reducing coder context overhead and
early-task drift.

Scout is **optional** (Runner decides per task) and **strictly
read-only** (no file mutations, no git operations).

## 2. Motivation

Coder context packets currently carry the task brief and references
to Den state, but the coder must independently discover the codebase
structure on every launch. For large or unfamiliar repos, this adds
significant context-window overhead and risks early drift when the
coder misreads the existing architecture.

A pre-coder Scout run can:

- Map the repository structure relevant to the task.
- Identify existing patterns, classes, functions, and test locations.
- Highlight risk areas (circular dependencies, stale code, nested
  conditionals).
- Detect duplicate or similar implementations the coder should reuse.
- Produce a deterministic structured artifact (`scout_report`) that
  the Runner can review and incorporate into the coder context packet.

## 3. Design principles

| Principle | Rationale |
|---|---|
| **Advisory, not gating** | Scout findings inform the coder but do not replace coder checkpoints or Runner judgement |
| **Strictly read-only** | Scout must never modify the codebase — enforced by toolset/profile restrictions |
| **High-context model** | Scout needs to read the full relevant codebase; same model class as coder |
| **Deterministic artifact** | Scout output is a structured `scout_report` with versioned schema |
| **Runner-controlled** | Runner decides whether to launch Scout, skip it, and how much scout output to pass to coder |
| **Zero memory** | Scout runs are independent; no memory carryover between runs |

## 4. Scout report schema

The Scout produces a structured `scout_report` as its completion artifact.
The artifact is posted as a `scout_report_packet` to Den.

### 4.1 Required top-level fields

```json
{
  "project_id": "den-hermes-bridge",
  "task_id": 1691,
  "run_id": "t1691-scout-...",
  "role": "scout",
  "status": "completed",
  "summary": "One-line human-readable summary of scout findings",
  "scout_report": {
    "report_version": "1.0",
    "surveyed_paths": [],
    "key_patterns": [],
    "existing_tests": [],
    "risk_areas": [],
    "recommended_approach": "",
    "duplicate_detection": [],
    "assumptions": []
  },
  "read_only_verified": true
}
```

### 4.2 Field definitions

| Field | Type | Required | Description |
|---|---|---|---|
| `report_version` | string | yes | Schema version, currently `"1.0"` |
| `surveyed_paths` | string[] | yes | Absolute or repo-relative paths inspected |
| `key_patterns` | object[] | yes | Array of discovered patterns (see below) |
| `existing_tests` | string[] | yes | Test files relevant to the task scope |
| `risk_areas` | string[] | yes | Potential pitfalls found during inspection |
| `recommended_approach` | string | yes | Brief guidance for the coder (≤500 chars) |
| `duplicate_detection` | string[] | no | Similar implementations to reuse |
| `assumptions` | string[] | yes | Assumptions made during inspection |

### 4.3 `key_patterns` entry schema

Each entry:

| Field | Type | Description |
|---|---|---|
| `file` | string | File path where pattern was found |
| `pattern` | string | Class or function name |
| `category` | string | One of `class`, `function`, `route`, `adapter`, `schema`, `config`, `test_pattern`, `service_boundary` |
| `summary` | string | Brief explanation (≤200 chars) |
| `line_number` | int | Optional — approximate line number |

### 4.4 Example scout report

```json
{
  "project_id": "den-hermes-bridge",
  "task_id": 1691,
  "run_id": "t1691-scout-abc123",
  "role": "scout",
  "status": "completed",
  "summary": "Surveyed 8 files in the bridge module. Found 3 service boundary classes, 2 existing adapters, 1 test file.",
  "scout_report": {
    "report_version": "1.0",
    "surveyed_paths": [
      "den_hermes/orchestrator.py",
      "den_hermes/runtime_registry.py",
      "den_hermes/worker_launcher.py",
      "den_hermes/pool_runtime.py",
      "docs/checkpoint-protocol-pilot.md",
      "tests/test_orchestrator.py"
    ],
    "key_patterns": [
      {
        "file": "den_hermes/orchestrator.py",
        "pattern": "DenWorkflowAdapter",
        "category": "service_boundary",
        "summary": "Primary adapter class coordinating Den MCP calls",
        "line_number": 185
      },
      {
        "file": "den_hermes/runtime_registry.py",
        "pattern": "resolve_role_runtime",
        "category": "function",
        "summary": "Resolves worker role to spawned-Hermes runtime config",
        "line_number": 115
      }
    ],
    "existing_tests": [
      "tests/test_runtime_registry.py",
      "tests/test_orchestrator.py"
    ],
    "risk_areas": [
      "CANONICAL_ROLES in runtime_registry.py does not include 'scout' — adding it requires code change",
      "Existing gate role paths in orchestrator.py do not handle scout packet type"
    ],
    "recommended_approach": "Add scout as a new canonical role, register it in the runtime registry, and extend orchestrator gate-role support for scout_report_packet type.",
    "duplicate_detection": [
      "Drift checker role has similar read-only contract — can reuse its profile/toolset pattern"
    ],
    "assumptions": [
      "Scout role will be added to CANONICAL_ROLES in runtime_registry.py",
      "Sample registry at config/spawned-hermes-runtimes.sample.yaml will be updated"
    ]
  },
  "blockers": [],
  "read_only_verified": true
}
```

## 5. Read-only enforcement

### 5.1 Profile-level enforcement

The Scout's Hermes profile (`spawned-scout`) must:

- Include only `file` and limited `terminal` tools.
- NOT include `write_file`, `patch`, `git` write commands.
- NOT expose any MCP write tools (no `mcp_den_send_message`,
  `mcp_den_post_*`).
- Use `memory_policy: zero_medium_term_zero_long_term`,
  `memory_enabled: false`.

### 5.2 Toolset restrictions

Allowed tools:
- `file` (read mode only: `read_file`, `search_files`)
- `terminal` (read-only commands: `cat`, `ls`, `head`, `find`,
  `grep`/`rg`, `git log`, `git diff`, `git show`, `git grep`,
  `git ls-tree`, `file`, `wc`)

Forbidden tools:
- `write_file`, `patch`
- `git commit`, `git push`, `git checkout -b`, `git merge`
- Any write-capable MCP tools

### 5.3 Artifact marker

Every Scout completion artifact MUST include `"read_only_verified": true`.
The bridge must reject any Scout artifact that lacks this field or has
`read_only_verified: false`.

## 6. How the Runner uses Scout in coder context packets

### 6.1 Integration flow

```
Step 1: Runner evaluates task. Decision: launch Scout?
  |-- YES: Run Scout worker with task context.
  |         Scout returns scout_report packet.
  |         Runner reads scout_report_message_id.
  |         Runner includes scout evidence in coder context packet notes.
  |-- NO:  Coder proceeds without scout evidence.
  |
Step 2: Runner prepares coder context packet.
  |-- With scout: includes `scout_report_message_id` and
  |   key scout findings in `notes` field.
  |-- Without scout: standard context packet.
  |
Step 3: Coder receives context packet.
  |-- With scout: coder acknowledges scout evidence in
  |   interpretation_checkpoint.content.scout_evidence.
  |-- Without scout: coder proceeds independently.
```

### 6.2 Context packet enrichment

When Scout is used, the coder context packet (prepared by
`DenWorkflowAdapter.prepare_coder_context_packet`) should include:

```python
notes: (
    "Scout report available at message_id=<id>. "
    "Key findings: surveyed <N> paths, <M> risk areas identified. "
    "See scout_report for full details."
)
```

The coder's `interpretation_checkpoint` can then reference scout
findings via the optional `content.scout_evidence` field defined
in `docs/checkpoint-protocol-pilot.md`.

### 6.3 Scout failure handling

If Scout fails or returns blocked:

- Runner receives a `blocked` or `failed` scout artifact with
  `blocker_summary` and `recovery_guidance`.
- Runner decides: retry Scout (with corrected context), skip Scout
  and proceed with coder, or block the task.
- Scout failure is **not** a fatal task error — the Runner chooses
  how to respond. This preserves the Scout role as advisory.

## 7. Checkpoint protocol integration

Scout participates minimally in the checkpoint protocol:

| Checkpoint type | Scout usage |
|---|---|
| `assignment_ack` | Scout sends this after launch, acknowledging task scope |
| `interpretation_checkpoint` | Scout does NOT send this — interpretation is the coder's domain |
| `plan_checkpoint` | Scout does NOT send this — planning is the coder's domain |
| `blocked_needs_input` | Scout sends this if it cannot complete its survey |

Scout sends `assignment_ack` to confirm identity and scope, then
proceeds directly to surveying the codebase. It does not require
checkpoint gating because:

- Scout is strictly read-only — there is no risk of wrong-direction
  code changes.
- Scout output is advisory — the Runner reviews the completed report,
  not an intermediate checkpoint.
- If Scout discovers a blocking condition (e.g. repo not found,
  permission denied), it sends `blocked_needs_input` instead of a
  partial report.

The `scout_report_packet` is a Den completion packet, not a checkpoint.
It follows the standard worker completion lifecycle:

1. Register Scout worker run.
2. Launch Scout Hermes subprocess.
3. Scout produces `completion.json` with `scout_report` payload.
4. Bridge verifies artifact identity, `scout_report` presence, and
   `read_only_verified: true`.
5. Bridge posts `scout_report_packet` to Den.
6. Runner reads report and proceeds to coder step.

## 8. Runtime registry entry

### 8.1 Proposed registry entry

For the central runtime registry (`/home/agents/runtime/spawned-hermes-runtimes.yaml`):

```yaml
roles:
  scout:
    runtime_id: scout-primary
    profile: spawned-scout
    provider: opencode-go
    model: glm-5.1
    toolsets: [terminal, file]
    timeout_seconds: 600
    reasoning_effort: high
    max_retries: 0
    preflight:
      enabled: true
    launch:
      source: den-worker
      extra_args: []
    memory_policy: zero_medium_term_zero_long_term
    memory_enabled: false
```

### 8.2 Profile requirements

The `spawned-scout` Hermes profile must be configured with:

- Read-only tool definitions (no write tools).
- `memory_policy: zero_medium_term_zero_long_term`.
- `memory_enabled: false`.
- Provider/model resolving to a high-context model (same as coder).

Currently, the active registry and sample carry only the five canonical
roles. Adding Scout requires:

1. Operator adds the Scout entry to the active registry.
2. Operator creates the `spawned-scout` Hermes profile (cloned from
   `spawned-coder` but with read-only toolsets).
3. A future coder task adds `scout` to `CANONICAL_ROLES` in
   `runtime_registry.py`.

### 8.3 Profile shape from available config

Based on the active runtime registry at
`/home/agents/runtime/spawned-hermes-runtimes.yaml`, the actual
provider/model strings for the coder role (which Scout would mirror)
are:

| Field | Current value (coder) | Scout equivalent |
|---|---|---|
| `profile` | `spawned-coder` | `spawned-scout` |
| `provider` | `opencode-go` | `opencode-go` |
| `model` | `glm-5.1` | `glm-5.1` |
| `toolsets` | `terminal, file` | `terminal, file` (read-only mode enforced by profile) |

These values match the actual active registry config verified during
task #1779 implementation.

## 9. Orchestrator integration

### 9.1 Current state

The orchestrator (`den_hermes/orchestrator.py`) currently supports
these orchestration action types:

```python
OrchestratorActionType = {
    START_CODER, AWAIT_CODER,
    START_REVIEWER, AWAIT_REVIEWER,
    START_VALIDATOR, AWAIT_VALIDATOR,
    START_DRIFT_CHECKER, AWAIT_DRIFT_CHECKER,
    START_PACKET_AUDITOR, AWAIT_PACKET_AUDITOR,
    HANDLE_CHANGES_REQUESTED, DONE, BLOCKED, FAILED,
}
```

There is no `START_SCOUT` or `AWAIT_SCOUT`. The gate role path
(`run_tracked_gate_role_path`) could be called with `role="scout"`
if the orchestrator dispatches it, but the orchestrator state machine
does not recognize Scout as a distinct action.

### 9.2 Required changes for full Scout integration

When Scout moves from design to implementation:

1. Add `START_SCOUT`, `AWAIT_SCOUT` to `OrchestratorActionType`.
2. Add `_packet_type_for_role("scout")` -> `"scout_report_packet"`
   in orchestrator.py.
3. Add `_prepare_packet_tool_for_role("scout")` mapping.
4. Extend the orchestrator state machine to optionally run Scout
   before the coder path when the task complexity or Runner flag
   warrants it.
5. Add a `scout_enabled` or `run_scout` field to the orchestrator
   workflow context.

These changes are **not implemented in this task** — they are
documented as follow-up recommendations.

## 10. Runner guidance: when to use Scout

### 10.1 Recommended

- **Large codebase areas**: when the task touches unfamiliar modules
  with >20 files.
- **Cross-service work**: when the change may span multiple service
  boundaries.
- **Architecture-sensitive tasks**: mandatory-checkpoint tasks where
  an early survey reduces drift risk.
- **Duplicate detection**: when the Runner suspects similar
  implementations exist that the coder should reuse.
- **Pattern extraction**: when the task requires following existing
  patterns (e.g. "add a new adapter following the pattern of existing
  adapters").

### 10.2 Not recommended

- **Documentation-only**: Scout produces no value for docs tasks.
- **Known-path fixes**: when the Runner knows exactly which file and
  function to change.
- **Urgent hotfixes**: Scout overhead (~5–10 minutes) may delay the
  fix.
- **Trivial scope**: single-file, single-function changes where the
  coder's own survey is sufficient.

## 11. Failure and block behavior

### 11.1 Scout fails completely

Scout cannot read the repo (e.g. missing worktree, permission denied).

```
→ Scout sends `blocked_needs_input` with:
    - blocker_summary: "Cannot access repository at <path>"
    - blocker_category: "infrastructure"
    - recovery_guidance: "Runner to verify worktree path and permissions"
```

Runner action: fix the path/permissions, retry Scout, or skip Scout
and launch coder directly.

### 11.2 Scout completes with empty report

Scout could not find relevant patterns (e.g. task scope does not
match any existing code).

```
→ Scout artifact has empty arrays for surveyed_paths, key_patterns, etc.
  status: "completed"
  summary: "No relevant patterns found in the surveyed area. Proceeding with default approach."
```

Runner action: the report documents the absence of existing patterns,
which is still valuable. Proceed to coder.

### 11.3 Scout times out

Scout exceeds `timeout_seconds` (default 600s).

```
→ Bridge posts `worker_failure_packet` with:
    - summary: "Scout worker timed out after 600 seconds"
    - failure_category: "spawned_hermes_timeout"
```

Runner action: if the codebase is unusually large, increase Scout
timeout. Otherwise, skip Scout and proceed to coder.

### 11.4 Scout artifact lacks `read_only_verified`

```
→ Bridge rejects artifact with:
    - error: "Scout artifact missing read_only_verified flag"
    - posts worker_failure_packet
```

Runner action: the Scout profile/toolset setup may be allowing write
operations. Inspect profile config and retry.

## 12. Safe live-smoke plan

A constrained live smoke of the Scout role should follow this procedure
after Scout implementation is complete:

### Preconditions

- [ ] `scout` added to `CANONICAL_ROLES` in `runtime_registry.py`.
- [ ] `spawned-scout` Hermes profile exists with read-only toolsets.
- [ ] Scout entry added to active runtime registry.
- [ ] Scout `scout_report_packet` type registered in orchestrator.
- [ ] All existing tests pass: `python -m pytest -q`
- [ ] Runtime registry validates: `python -m den_hermes.runtime_ops validate`
- [ ] Scout preflight passes: `python -m den_hermes.runtime_ops preflight --roles scout`

### Smoke steps

1. **Disposable task**: Use a non-production task (e.g. a docs task on
   a cloned branch) to avoid interference.
2. **Launch Scout** via `run_tracked_gate_role_path(role="scout", ...)`.
3. **Verify registration**: Scout worker run registered in Den before
   subprocess launch.
4. **Verify report**: Scout artifact has all required `scout_report`
   fields and `read_only_verified: true`.
5. **Verify read-only**: Confirm no file mutations occurred (git status
   is clean, no new files created).
6. **Readback**: Den has `scout_report_packet` visible via
   `get_latest_worker_completion`.
7. **Enrich coder context**: Prepare coder context packet referencing
   scout report message ID.
8. **Launch coder**: Verify coder acknowledges scout evidence in
   interpretation_checkpoint.
9. **Record lesson**: Capture smoke result fields.

### Smoke lesson fields

| Field | Example |
|---|---|
| `lesson_id` | `scout-smoke-1691-20260531` |
| `lesson.source` | `spawned-scout-design-smoke` |
| `lesson.verdict` | `passed` / `blocked` / `failed` |
| `lesson.timestamp` | ISO 8601 |
| `lesson.run_id` | Scout run ID |
| `lesson.report_valid` | `true` / `false` |
| `lesson.read_only_confirmed` | `true` / `false` |
| `lesson.notes` | Free-text observations |

## 13. Cross-reference: #1779 worker role catalog

This Scout design is referenced from the worker role catalog at
`docs/worker-role-catalog.md` (§3.6 Scout, §8 Scout-specific role
contract, §9 Cross-reference). The catalog provides the complete
six-role matrix; this document provides the Scout-specific depth.

## 14. Follow-up implementation tasks

The following concrete changes are needed to operationalize Scout
but are **outside the scope of #1691/#1779** (design/documentation):

| # | Task | Scope | Priority |
|---|---|---|---|
| 1 | Add `scout` to `CANONICAL_ROLES` in `den_hermes/runtime_registry.py` | Code | High |
| 2 | Add `scout` runtime entry to `/home/agents/runtime/spawned-hermes-runtimes.yaml` | Ops | High |
| 3 | Create `spawned-scout` Hermes profile with read-only toolsets | Ops | High |
| 4 | Add `START_SCOUT`, `AWAIT_SCOUT` to `OrchestratorActionType` | Code | Medium |
| 5 | Add `scout_report_packet` to packet type mapping in orchestrator.py | Code | Medium |
| 6 | Add `scout` alias to `role_aliases` in runtime registry | Ops | Low |
| 7 | Add scout report validation script (similar to `validate_denops_receipt.py`) | Code | Low |
| 8 | Extend `run_den_coder_reviewer_workflow` for optional scout pre-step | Code | Low |

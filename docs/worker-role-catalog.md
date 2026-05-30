# Spawned-Hermes worker role catalog

Task: `den-hermes-bridge` #1779
Related: `den-hermes-bridge` #1691 (Scout role design)
Parent: `den-core` #1778 worker-pool operationalization

## 1. Purpose

This document defines the complete worker role catalog for spawned-Hermes
workflows managed by the Den orchestrator bridge. Each role entry defines:

- Profile identity convention and `worker_role` value.
- Capability tags for routing and discovery.
- Allowed toolset and side-effect envelope.
- Expected checkpoint and packet types.
- Model/provider class guidance.
- Cleanup/release expectations.
- Identity and instance rules.

## 2. Role profile identity conventions

Every spawned-Hermes worker role uses a **shared Hermes profile**. The
profile identity is a grouping handle; concrete instance selection uses
`pool_member_id` or `agent_instance_id`.

| Role | `profile_identity` | `worker_role` (Den value) | Canonical Den role |
|---|---|---|---|
| Coder | `spawned-coder` | `coder` | `coder` |
| Reviewer | `spawned-reviewer` | `reviewer` | `reviewer` |
| Validator | `spawned-validator` | `validator` | `validator` |
| Drift Checker | `spawned-drift-checker` | `drift_checker` | `drift_checker` |
| Packet Auditor | `spawned-packet-auditor` | `packet_auditor` | `packet_auditor` |
| Scout | `spawned-scout` | `scout` | `scout` |

### Identity rules

- **Shared profile identity**: role profiles are shared across all instances
  of that role. Never create `spawned-coder-01`, `spawned-coder-bob`, etc.
  Duplicate profiles would violate the central runtime registry and break
  the role/source-of-truth contract. (See `docs/spawned-role-pool-member-identity.md`.)
- **`pool_member_id`**: concrete instance identifier within a pool, e.g.
  `pool-coder-01`. Required for pool members; optional for one-shot workers.
- **`agent_instance_id`**: system-level unique handle for the process/lifecycle,
  e.g. `hermes:den-k8:spawned-coder:wake-a1b2c3`. Used in Gateway delivery
  metadata and Core agent-instance bindings.
- **Delivery targeting**: when delivering to a shared-profile worker pool,
  the delivery carries a `concrete_identity` (`pool_member_id` or
  `agent_instance_id`). The bridge selects exactly one matching binding.
  Ambiguous or zero matches fail closed.

### Runtime registry aliases

| Alias | Canonical role |
|---|---|
| `drift` | `drift_checker` |
| `audit` | `packet_auditor` |

No aliases are defined for `coder`, `reviewer`, `validator`, or `scout`
(see §9 for Scout alias discussion).

## 3. Role definitions

### 3.1 Coder

| Field | Value |
|---|---|
| Profile identity | `spawned-coder` |
| Worker role | `coder` |
| Capability tags | `implementation`, `code_generation` |
| Allowed toolsets | `terminal`, `file` (or narrower when scoped) |
| Side-effect envelope | **Mutating**: writes source files, commits to task branch |
| Checkpoint types | `assignment_ack`, `interpretation_checkpoint`, `plan_checkpoint`, `partial_result_checkpoint`, `blocked_needs_input` |
| Packet type | `implementation_packet` |
| Model/provider class | High-context, coding-strong (e.g. `glm-5.1`, `gpt-5.5`) |
| Reasoning effort | `high` recommended |
| Cleanup/release | Standard: scrub workspace, release process, rotate session, clean scratch |
| Artifact required fields | `branch`, `head_commit`, `tests_run`, optional `base_commit`, `claimed_finding_ids` |
| Memory policy | Zero medium-term, zero long-term |

**Contract**: A coder receives a bounded context packet, writes deterministic
source-file changes on the task branch, runs relevant tests, and produces
a structured completion JSON. The bridge verifies artifact identity, git
branch/head evidence, and posts `implementation_packet` before requesting
review.

### 3.2 Reviewer

| Field | Value |
|---|---|
| Profile identity | `spawned-reviewer` |
| Worker role | `reviewer` |
| Capability tags | `review`, `code_audit` |
| Allowed toolsets | `terminal`, `file` (read-write for inspecting changes) |
| Side-effect envelope | **Read-heavy**: reads source/tests, makes no code changes |
| Checkpoint types | `assignment_ack`, `blocked_needs_input` |
| Packet type | `review_findings_packet` |
| Model/provider class | High-context, review-strong (e.g. `deepseek-v4-flash`, `gpt-5.5`) |
| Reasoning effort | `high` recommended |
| Cleanup/release | Standard |
| Artifact required fields | `verdict` (one of `looks_good`, `changes_requested`, `blocked`), `findings` array, optional `finding_ids`, `review_round_id` |
| Memory policy | Zero medium-term, zero long-term |

**Contract**: A reviewer receives the coder's completion evidence (branch,
head_commit, tests_run, summary), inspects the changes, and produces a
structured completion with verdict and findings. When `verdict` is
`changes_requested`, the coder re-enters the workflow loop.

**Verdict rule**: If any required check/test/hygiene command reports
non-zero exit or failure, the verdict must not be `looks_good`. Put only
blocking or actionable findings in the structured findings array.

### 3.3 Validator

| Field | Value |
|---|---|
| Profile identity | `spawned-validator` |
| Worker role | `validator` |
| Capability tags | `validation`, `test_verification` |
| Allowed toolsets | `terminal`, `file` |
| Side-effect envelope | **Read-only** for most operations; may run tests |
| Checkpoint types | `assignment_ack`, `blocked_needs_input` |
| Packet type | `validation_packet` |
| Model/provider class | Medium-context, deterministic (e.g. `kimi-k2.6`, `gpt-5.5`) |
| Reasoning effort | `medium` |
| Cleanup/release | Standard |
| Artifact required fields | Validation summary, `tests_run` or explicit validation commands/results |
| Memory policy | Zero medium-term, zero long-term |

**Contract**: A validator checks that the coder's output meets acceptance
criteria. Runs validation suites, checks test output, confirms that
definition-of-done is met. Produces a structured validation summary.

### 3.4 Drift Checker

| Field | Value |
|---|---|
| Profile identity | `spawned-drift-checker` |
| Worker role | `drift_checker` |
| Capability tags | `drift_detection`, `consistency_check` |
| Allowed toolsets | `terminal`, `file` (read-only) |
| Side-effect envelope | **Read-only** |
| Checkpoint types | `assignment_ack`, `blocked_needs_input` |
| Packet type | `drift_check_packet` |
| Model/provider class | Medium-context, pattern-matching strong |
| Reasoning effort | `medium` |
| Cleanup/release | Standard |
| Artifact required fields | Drift verdict/status, checked refs/packets, notes |
| Memory policy | Zero medium-term, zero long-term |

**Contract**: A drift checker verifies that the codebase or task-thread
state has not diverged from the expected plan/checkpoint. Checks refs,
packets, and consistency markers. Reports `no_drift`, `minor_drift`, or
`significant_drift`.

### 3.5 Packet Auditor

| Field | Value |
|---|---|
| Profile identity | `spawned-packet-auditor` |
| Worker role | `packet_auditor` |
| Capability tags | `audit`, `packet_verification` |
| Allowed toolsets | `file` (read-only) |
| Side-effect envelope | **Read-only** |
| Checkpoint types | `assignment_ack`, `blocked_needs_input` |
| Packet type | `packet_audit_packet` |
| Model/provider class | Medium-context, schema-verification oriented (e.g. `gpt-5.5`) |
| Reasoning effort | `medium` |
| Cleanup/release | Standard |
| Artifact required fields | Audited packet refs, verdict/status, notes |
| Memory policy | Zero medium-term, zero long-term |

**Contract**: A packet auditor inspects completion packets, review
packets, context packets, or checkpoint messages for structural
correctness, field presence, and identity consistency. Does not assess
code quality — only packet integrity.

### 3.6 Scout

| Field | Value |
|---|---|
| Profile identity | `spawned-scout` |
| Worker role | `scout` |
| Capability tags | `discovery`, `codebase_inspection`, `pre_coder_analysis` |
| Allowed toolsets | `terminal`, `file` (read-only) |
| Side-effect envelope | **Strictly read-only** — no file writes, no git mutations, no branching |
| Checkpoint types | `assignment_ack`, `blocked_needs_input` |
| Packet type | `scout_report` (see `docs/spawned-scout-design-1691.md`) |
| Model/provider class | **High-context model strongly recommended** — Scout reads the entire relevant codebase surface before the coder. Provider/model should be chosen for maximum context window and analysis quality rather than speed. Suggested: `glm-5.1` (same as coder) or equivalent high-context provider. |
| Reasoning effort | `high` recommended |
| Cleanup/release | Standard (minimal since no mutations) |
| Artifact required fields | `scout_report` (defined in §8), read-only evidence markers |
| Memory policy | Zero medium-term, zero long-term |
| Den role registration | `scout` (proposed addition to `CANONICAL_ROLES`) |

**Full contract**: See `docs/spawned-scout-design-1691.md`.

## 4. Side-effect envelope summary

| Role | Mutating | Effect level |
|---|---|---|
| Coder | Yes | Mutating |
| Reviewer | No | Read-heavy (no code changes) |
| Validator | No (runs tests, no code changes) | Read-heavy |
| Drift Checker | No | Read-only |
| Packet Auditor | No | Read-only |
| Scout | No | Strictly read-only |

## 5. Checkpoint and packet type reference

### 5.1 Checkpoint types (intermediate coordination)

All checkpoints are Den task-thread messages with `metadata.type` values.
See `docs/checkpoint-protocol-pilot.md` for full templates.

| Checkpoint type | Required by roles | Purpose |
|---|---|---|
| `assignment_ack` | All roles | Confirm identity and task understanding |
| `interpretation_checkpoint` | Coder (mandatory for complex tasks) | Interpretation before implementation |
| `plan_checkpoint` | Coder (mandatory for complex tasks) | Exact files/approach before editing |
| `checkpoint_response` | Runner | Approve/correct/block a checkpoint |
| `partial_result_checkpoint` | Coder | Vertical-slice checkpoint during broad work |
| `blocked_needs_input` | All roles | Typed blocker with recovery guidance |

### 5.2 Den completion packet types

| Role | Packet type | Den `packet_type` value |
|---|---|---|
| Coder | Implementation packet | `implementation_packet` |
| Reviewer | Review findings packet | `review_findings_packet` |
| Validator | Validation packet | `validation_packet` |
| Drift Checker | Drift check packet | `drift_check_packet` |
| Packet Auditor | Packet audit packet | `packet_audit_packet` |
| Scout | Scout report packet | `scout_report_packet` |
| Any (infrastructure failure) | Worker failure packet | `worker_failure_packet` |

## 6. Model/provider class guidance

| Role | Profile | Provider | Model(s) | Rationale |
|---|---|---|---|---|
| Coder | `spawned-coder` | `opencode-go` | `glm-5.1` or equivalent | Highest coding quality, large context |
| Reviewer | `spawned-reviewer` | `opencode-go` | `deepseek-v4-flash` or equivalent | Fast review, strong reasoning |
| Validator | `spawned-validator` | `opencode-go` | `kimi-k2.6` | Deterministic validation, medium cost |
| Drift Checker | `spawned-drift-checker` | `opencode-go` | `kimi-k2.6` | Pattern-matching/cost tradeoff |
| Packet Auditor | `spawned-packet-auditor` | `openai-codex` | `gpt-5.5` | Schema/structural verification |
| Scout | `spawned-scout` | `opencode-go` | `glm-5.1` or equivalent | High-context codebase analysis |

These are default picks from the central runtime registry
(`/home/agents/runtime/spawned-hermes-runtimes.yaml`). Operators modify
the central registry to change model assignments; per-role overrides
require explicit emergency escape-hatch flags.

## 7. Cleanup and release expectations

All spawned-Hermes worker roles follow the same cleanup/release lifecycle
defined in `docs/worker-pool-mvp-rollout-runbook.md`:

1. **Terminal state** (`COMPLETED`, `BLOCKED`, or `FAILED`).
2. **Cleanup evidence** requires four boolean fields:
   - `scrub_workspace`: worker workspace files released
   - `process_release`: child processes terminated
   - `session_rotation`: Hermes session rotated/closed
   - `scratch_cleanup`: temp files/dirs removed
3. **Release** after complete cleanup evidence → worker can accept new
   assignments.
4. **Quarantine** if cleanup evidence is incomplete → operator must
   manually inspect and resolve.

Read-only roles (Scout, Drift Checker, Packet Auditor) have simpler
cleanup since they produce no source-file mutations. However, the same
four-field evidence contract applies — even read-only workers may
accumulate temp files, process handles, or session state.

## 8. Scout-specific role contract

This section defines the Scout role contract. The full design document
for Scout (#1691) is at `docs/spawned-scout-design-1691.md`.

### 8.1 Required vs skipped use

**Use Scout when:**
- The task involves an unfamiliar codebase area and the coder would benefit
  from a pre-discovery report.
- The repository is large and a pre-coder directory/file map would reduce
  coder context overhead.
- Multiple files are likely to be touched and an early survey of existing
  patterns, tests, and service boundaries would prevent drift.
- The Runner wants a deterministic artifact proving codebase state before
  the coder starts.

**Skip Scout when:**
- The task is documentation-only, ops-only, or a known-path fix.
- The codebase area is well-known to the coder from prior rounds.
- The task is urgent and the scout overhead would exceed its benefit.
- The task scope is trivially narrow (single file, single function).

### 8.2 Scout report schema

The Scout produces a `scout_report` structured artifact (posted as
`scout_report_packet` to Den). The report schema:

```json
{
  "project_id": "den-hermes-bridge",
  "task_id": 1691,
  "run_id": "t1691-scout-...",
  "role": "scout",
  "status": "completed",
  "summary": "One-line report summary",
  "scout_report": {
    "surveyed_paths": [
      "path/to/relevant/file1.py",
      "path/to/relevant/file2.py"
    ],
    "key_patterns": [
      {"file": "path/to/file.py", "pattern": "ClassName", "summary": "service boundary class"}
    ],
    "existing_tests": [
      "tests/test_something.py"
    ],
    "risk_areas": [
      "Area of concern and why"
    ],
    "recommended_approach": "Brief guidance for the coder",
    "duplicate_detection": [
      "Similar existing implementations or patterns to reuse"
    ],
    "assumptions": [
      "Assumptions made during inspection"
    ],
    "scout_report_version": "1.0"
  },
  "blockers": [],
  "read_only_verified": true
}
```

### 8.3 Read-only permissions

Scout is **strictly read-only**:
- No `write_file`, `patch`, or file-write tools.
- No `git commit`, `git push`, `git branch`, `git checkout -b`.
- No MCP write operations (no message posting, no task state mutation).
- Only `file`, `terminal` (read-only commands: `cat`, `ls`, `head`, `git log`,
  `git diff`, `git show`, `git grep`, `find`, `rg`, `grep`).
- The Hermes profile for Scout should omit write-capable tool definitions.

### 8.4 Integration with interpretation_checkpoint and plan_checkpoint

The Scout report feeds into the coder's checkpoint protocol:

1. **Runner decides** whether to launch a Scout before the coder.
2. **Scout runs** and posts `scout_report_packet` to the Den task-thread.
3. **Runner reads** the scout report and includes its key findings in the
   coder context packet (or references it by Den message ID).
4. **Coder receives** context packet with scout evidence included.
5. **Coder's `interpretation_checkpoint`** should reference scout findings
   in `content.scout_evidence`.
6. **Coder's `plan_checkpoint`** should reflect scout-informed file/directory
   awareness.

Scout does not gate the coder — the Runner may skip, read, or incorporate
scout output at discretion. The coder must always post mandatory checkpoints
independently; scout evidence supplements but does not replace them.

### 8.5 Failure and block behavior

- **Scout fails**: if Scout cannot read the repo (permissions, missing paths)
  or produces an empty report, it posts a `blocked` or `failed` artifact
  with the reason. The Runner decides whether to launch the coder anyway
  or retry Scout.
- **Scout completes but empty**: if the surveyed area has no relevant
  patterns, the report's arrays are empty with a note. This is a valid
  completed state — the coder proceeds without scout context.
- **Runner bypasses Scout**: if Scout fails or is skipped, the coder
  proceeds without scout evidence. Scout is advisory, not mandatory.

### 8.6 Runtime registry entry

The Scout role should be registered in the central runtime registry under
`roles.scout`. The sample registry at `config/spawned-hermes-runtimes.sample.yaml`
should include a scout entry.

Proposed registry entry:

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

### 8.7 Runner integration

The Runner uses Scout in the coder context packet construction:

1. Optionally launches a Scout worker via `run_tracked_gate_role_path`
   with `role="scout"`.
2. Reads the `scout_report` from the scout's completion artifact.
3. Populates the coder context packet with scout evidence, either inline
   or by reference (`scout_report_message_id`).
4. Launches the coder worker with the enriched context packet.

If the Runner is using checkpoint protocol (mandatory for complex tasks),
the scout report should be referenced in the coder context packet's
`notes` field or as an explicit `scout_evidence` section.

## 9. Cross-reference: #1691 Scout role design

The scout role design originates from Den task #1691. Key design decisions:

- Scout is a **separate role** (not a coder mode) because it needs
  different capability tags, toolset restrictions, and artifact schema.
- Scout is **optional** — the Runner decides per task whether scout
  overhead is justified.
- Scout uses a **high-context model** (same as coder) because it needs
  to read the full codebase surface and produce meaningful analysis.
- The scout report is **advisory** — the coder makes independent decisions
  and must still post mandatory checkpoints.
- Scout is **read-only by profile** — its profile should lack write tools
  to enforce the side-effect contract at the profile/toolset level.

### 9.1 Follow-up recommendations from #1779

To fully integrate Scout as an operational role, these follow-up tasks
are recommended (not implemented here):

1. **Add `scout` to `CANONICAL_ROLES`** in `den_hermes/runtime_registry.py`
   (not done here — requires a separate coder task).
2. **Add `scout` runtime entry** to `/home/agents/runtime/spawned-hermes-runtimes.yaml`
   (operator action after task is accepted).
3. **Create `spawned-scout` Hermes profile** with read-only toolset
   (operator action).
4. **Add Scout to orchestrator action types** in `den_hermes/orchestrator.py`
   (`START_SCOUT`, `AWAIT_SCOUT`).
5. **Add `scout_report_packet`** to `_packet_type_for_role()` in orchestrator.py.
6. **Extend `run_den_coder_reviewer_workflow`** to support optional scout
   pre-step.
7. **Add `scout` alias** to `role_aliases` in the runtime registry.
8. **Add scout report validation** similar to existing receipt validators.

These follow-ups are outside the scope of task #1779's bounded deliverable.

## 10. Role summary matrix

| Role | Profile | Den role | Read-only? | Checkpoints | Packet type | Model class |
|---|---|---|---|---|---|---|
| Coder | `spawned-coder` | `coder` | No | `assignment_ack`, `interpretation`, `plan`, `partial_result`, `blocked` | `implementation_packet` | High-context coding (glm-5.1) |
| Reviewer | `spawned-reviewer` | `reviewer` | Near-read | `assignment_ack`, `blocked` | `review_findings_packet` | High-context review (deepseek-v4-flash) |
| Validator | `spawned-validator` | `validator` | Read-heavy | `assignment_ack`, `blocked` | `validation_packet` | Medium-context (kimi-k2.6) |
| Drift Checker | `spawned-drift-checker` | `drift_checker` | Read-only | `assignment_ack`, `blocked` | `drift_check_packet` | Medium-context (kimi-k2.6) |
| Packet Auditor | `spawned-packet-auditor` | `packet_auditor` | Read-only | `assignment_ack`, `blocked` | `packet_audit_packet` | Medium-context (gpt-5.5) |
| Scout | `spawned-scout` | `scout` | Strictly read-only | `assignment_ack`, `blocked` | `scout_report_packet` | High-context (glm-5.1) |

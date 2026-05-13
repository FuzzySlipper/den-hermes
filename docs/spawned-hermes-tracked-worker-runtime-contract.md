# Tracked spawned-Hermes worker runtime contract

Task: `den-hermes-bridge` #1373  
Status: implementation contract for child tasks #1374-#1381

## 1. Purpose

`spawned_hermes` is a Den worker substrate for local Hermes subprocesses. It replaces the brittle Pi/tmux/docker runtime for selected workflows while preserving Den's authoritative worker-run and completion-packet invariants.

The key invariant is non-negotiable:

> No authoritative worker completion packet may be accepted unless the `run_id` / `session_id` is already tracked as a Den worker run.

A spawned Hermes process exiting with code 0, writing prose, or returning a local artifact is not sufficient by itself. Success requires:

1. Den has a registered worker-run record for the local spawned worker.
2. The worker or bridge produces a structured role-specific artifact.
3. The bridge verifies identity and role-specific evidence.
4. The bridge posts a Den completion packet for the tracked run.
5. Den accepts/reconciles the packet and exposes it through worker status/latest-completion APIs.

## 2. Substrate boundary

This contract is for local spawned Hermes processes launched by `den-hermes-bridge`, not Pi containers. It must not assume Docker, tmux, Pi state directories, rootless Docker sockets, or server-side session renderers.

The substrate identifier is:

```text
spawned_hermes
```

The bridge may still borrow Den Pi concepts: bounded packet references, role-specific completion packets, fail-closed reconciliation, abort/cleanup/rerun, and auditability.

## 3. Run identity

Every spawned-Hermes worker run MUST have a Den registration before process launch.

Required identity fields:

| Field | Owner | Notes |
| --- | --- | --- |
| `project_id` | Den + bridge | Den project, e.g. `den-hermes-bridge`. |
| `task_id` | Den + bridge | Den task the worker is executing/reviewing/validating. |
| `run_id` | Den allocates or bridge supplies with Den acceptance | Stable worker-run identity used by completion/status tools. |
| `session_id` | Den allocates or bridge supplies with Den acceptance | Runtime/session handle. Can equal or derive from `run_id` if no separate Hermes session ID is available at registration time. |
| `role` | bridge request, Den validates | `coder`, `reviewer`, `validator`, `drift_checker`, `packet_auditor`, or future Den-recognized role. |
| `substrate` | bridge request, Den validates | Must be `spawned_hermes`. |
| `requested_by` | bridge | Usually `den-hermes-runner`. |
| `dedupe_key` | bridge | Used for retry-safe registration. |

Recommended identity rules:

- Den is authoritative for whether a run exists.
- A locally generated `run_id` has no authority until Den registration succeeds.
- Completion posting MUST fail closed if Den returns `missing_run`, `malformed`, `rejected`, or any `failure_category`.
- `run_id` and `session_id` must be literal values, never shell expressions or placeholders.

## 4. Launch metadata

The registration call should capture enough launch metadata for audit, status projection, rerun, and operator debugging without exposing secrets.

Required or strongly recommended launch fields:

| Field | Required? | Notes |
| --- | --- | --- |
| `host` | yes | Hostname or stable runner identifier. |
| `cwd` / `workdir` | yes | Process working directory. |
| `branch` | role-dependent | Requested implementation/review branch. |
| `base_branch` | role-dependent | Intended diff base. |
| `base_commit` | role-dependent | Base commit expected by orchestrator. |
| `head_commit` | optional at launch | Guidance or expected head for review/validation roles. |
| `profile` | yes if set | Hermes profile name only; no profile contents or secrets. |
| `provider` | yes if set | Provider name only. |
| `model` | yes if set | Model string. |
| `toolsets` | yes | Comma/list of enabled toolsets for the worker process. |
| `timeout_seconds` | yes | Runtime timeout recorded before launch. |
| `prompt_packet_message_id` | preferred | Den task-thread packet ref. Prefer this over large prompt args. |
| `state_file_ref` | optional | Den/state-file ref if used. |
| `expected_artifact_path` | yes | Local artifact path the bridge will validate. |
| `stdout_path` / `stderr_path` / `log_path` | yes when captured | Local paths for process diagnostics. |

Security requirements:

- Do not store raw prompts that may contain secrets if a Den packet reference is sufficient.
- Do not store API keys, auth tokens, env dumps, profile file contents, or full config files in Den metadata.
- Provider/profile names are allowed; credentials are resolved by the local Hermes process/profile.
- Log and artifact paths should be paths, not contents, unless explicitly sanitized.

## 5. Process metadata

Process metadata can be absent at initial registration if the process has not been spawned yet, but it must be updatable after launch.

Fields:

| Field | Owner | Notes |
| --- | --- | --- |
| `pid` | bridge | Local OS PID after successful `subprocess.Popen`. |
| `process_group_id` | bridge | Optional, useful for aborting children. |
| `session_handle` | bridge | Optional Hermes CLI session ID if discoverable. |
| `started_at` | Den and/or bridge | Set when launch begins or process starts. |
| `ended_at` | bridge/Den | Set when process exits or run is terminal. |
| `exit_code` | bridge | Process exit code; not success by itself. |
| `termination_signal` | bridge | If killed/aborted. |
| `last_heartbeat_at` | future optional | If spawned workers later emit heartbeats. |

`pid` is best-effort. A registered run without a PID can be valid in `registered` state, but a run in `running` state should have either `pid` or an explicit unavailable reason.

## 6. Artifact and packet metadata

The bridge-owned local artifact remains useful for fail-closed verification, but Den's completion packet is the authoritative lifecycle receipt.

Expected artifact fields common to all roles:

```json
{
  "project_id": "den-hermes-bridge",
  "task_id": 123,
  "run_id": "spawned-hermes-...",
  "role": "coder",
  "status": "completed",
  "summary": "Safe human-readable summary"
}
```

Role-specific artifact requirements:

| Role | Required fields |
| --- | --- |
| `coder` | `branch`, `head_commit`, `tests_run`, optional `base_commit` |
| `reviewer` | `verdict`, `findings`, optional `finding_ids`, optional `review_round_id` |
| `validator` | validation summary, `tests_run` or explicit validation commands/results |
| `drift_checker` | drift verdict/status, checked refs/packets, notes |
| `packet_auditor` | audited packet refs, verdict/status, notes |

The bridge must verify:

- artifact JSON parses;
- `project_id` if present matches launch project;
- `task_id`, `run_id`, and `role` match registration;
- role-specific required fields exist;
- coder `branch` / `head_commit` evidence is locally or remotely verifiable before requesting review;
- reviewer findings are materialized in Den before setting review verdict where applicable.

Completion packet mapping:

| Role | Den packet type |
| --- | --- |
| `coder` | `implementation_packet` |
| `reviewer` | `review_findings_packet` |
| `validator` | `validation_packet` |
| `drift_checker` | `drift_check_packet` |
| `packet_auditor` | `packet_audit_packet` |
| infrastructure failure | `worker_failure_packet` |

## 7. Lifecycle states

Den should expose a compact state projection that works for Pi and spawned-Hermes runs. Suggested spawned-Hermes states:

```text
registered -> starting -> running -> completion_posted -> completed
                                      -> failed
                                      -> blocked
                                      -> needs_input
                                      -> incomplete
registered -> aborted -> cleaned_up
starting/running -> abort_requested -> aborted -> cleaned_up
completed/failed/blocked/needs_input/incomplete/aborted -> cleaned_up
terminal -> rerun_requested -> registered/running for a new run
```

State definitions:

| State | Meaning |
| --- | --- |
| `registered` | Den run exists; process not spawned yet. |
| `starting` | Bridge is spawning process and preparing logs/artifact paths. |
| `running` | Process is active or presumed active. |
| `completion_posted` | Bridge called completion endpoint; Den is reconciling/recording. This may be transient. |
| `completed` | Den accepted successful role packet. |
| `failed` | Infrastructure or role failure packet accepted, or process failure recorded. |
| `blocked` | Worker reported blocked with recovery guidance. |
| `needs_input` | Worker requires human/planner input. |
| `incomplete` | Process ended but artifact/packet evidence is insufficient. |
| `abort_requested` | Operator requested termination; bridge should signal process. |
| `aborted` | Process/run was stopped before normal completion. |
| `cleaned_up` | Local process/log cleanup is complete or idempotently confirmed. |
| `rerun_requested` | Operator requested a fresh run based on prior launch metadata. |

Transition rules:

- Registration failure prevents process launch.
- Process launch failure should produce a tracked failure packet if registration succeeded; otherwise a task-thread diagnostic is the fallback.
- Process exit code 0 may transition only to `incomplete` until artifact verification and Den completion reconciliation succeed.
- Den completion rejection keeps the bridge in failure state and must surface as an orchestration failure.
- Cleanup is idempotent and may be called on terminal states.
- Rerun should create or register a new run unless Den explicitly supports reusing a run with an incremented attempt number.

## 8. API responsibility split

### Den server / MCP responsibilities

Den should own durable records and invariants:

- register a worker run for `substrate="spawned_hermes"`;
- validate project/task existence, role, substrate, and dedupe key;
- store identity, launch metadata, and status projection;
- provide update/status/abort/cleanup/rerun tools for local spawned runs where possible;
- reject completion packets for missing or mismatched runs;
- expose latest completion and worker status consistently with Pi worker runs;
- maintain audit trail for registration, status changes, completion packets, and cleanup.

Likely MCP/API additions or generalizations:

- `mcp_den_register_worker_run` or `mcp_den_register_spawned_hermes_worker_run`;
- optional `mcp_den_update_worker_run_runtime` for PID/log/artifact/exit metadata;
- status/abort/cleanup/rerun paths generalized from raw Pi worker tools or made substrate-aware.

### `den-hermes-bridge` responsibilities

The bridge should own local process mechanics and fail-closed verification:

- create bounded launch metadata;
- call Den registration before spawning Hermes;
- never spawn when registration fails;
- spawn Hermes with role/profile/provider/model/toolset/workdir/timeout args;
- set safe env vars such as `DEN_PROJECT_ID`, `DEN_TASK_ID`, `DEN_RUN_ID`, `DEN_WORKER_ROLE`, and `DEN_EXPECTED_ARTIFACT`;
- capture stdout/stderr/log paths;
- update runtime metadata with PID/exit code when Den APIs exist;
- validate artifact identity and role-specific fields;
- verify git branch/head when required;
- post Den completion packets and fail closed on rejection;
- request review and materialize findings only after authoritative completion gates pass;
- implement local abort/cleanup/rerun mechanics once Den exposes or records the relevant state.

## 9. Abort, cleanup, and rerun behavior

### Status

Status should combine Den state plus bridge-known runtime facts:

- Den registration and lifecycle state;
- PID/session handle if known;
- process alive/dead check when local and available;
- latest completion packet state;
- artifact path existence and verification status if applicable;
- log paths for debugging.

### Abort

Abort is best-effort but audited:

1. Den records `abort_requested`.
2. Bridge sends SIGTERM to process group or PID.
3. After grace period, bridge sends SIGKILL if still alive.
4. Bridge records exit/termination metadata.
5. Den moves to `aborted` and optionally accepts a failure/abort packet.

If the process has already ended, abort should be idempotent and report terminal state.

### Cleanup

Cleanup is idempotent:

- ensure no live process remains;
- optionally remove temporary prompt/artifact/log files according to retention policy;
- preserve enough metadata for audit;
- mark `cleaned_up` or record why cleanup is partial.

Default retention should keep logs/artifacts through at least the review window; destructive deletion should be explicit.

### Rerun

Rerun should:

- use prior launch metadata as a template;
- allocate/register a new run ID unless Den supports attempts explicitly;
- preserve parent/previous run linkage;
- not overwrite old logs/artifacts;
- require the same registration-before-spawn gate.

## 10. Checkpoint test strategy

The remaining child tasks should use RED/GREEN checkpoint tests in this order.

### #1374 — Den registration API

Server/API tests:

- fresh DB can register `substrate="spawned_hermes"` worker run;
- migration/ensure path creates required schema/indexes idempotently;
- invalid project/task/role/substrate is rejected;
- duplicate `dedupe_key` returns the existing run rather than creating duplicates;
- completion packet for registered run is accepted;
- completion packet for unregistered run remains rejected as `missing_run`;
- status projection includes substrate, role, task, lifecycle state, and latest completion.

### #1375 — `DenMcpAdapter` registration client

Unit tests with recording tools:

- adapter sends exact registration payload before completion;
- registration response is normalized into `run_id` / `session_id` handle;
- Den registration rejection raises and prevents launch path from proceeding;
- completion rejection still raises fail-closed;
- metadata excludes secrets and includes substrate/profile/provider/model/toolsets/workdir/artifact/log paths.

### #1376 — launcher registration before spawn

Fake subprocess tests:

- `register_worker_run` is called before `subprocess.run` / `Popen`;
- no process launches when registration fails;
- env vars contain Den identity from registration, not unaccepted local guesses;
- log/artifact paths are deterministic and per-run;
- timeout and exit-code failures produce tracked failure packets after successful registration.

### #1377 — completion reconciliation

Integration tests with fake or live-safe Den adapter:

- registered coder artifact posts accepted `implementation_packet`;
- registered reviewer artifact posts accepted `review_findings_packet`;
- missing/mismatched run IDs fail closed;
- `get_latest_worker_completion` and worker status reflect accepted packet.

### #1378 — lifecycle controls

Tests:

- status reports registered/running/completed/failed/aborted/cleaned states;
- abort terminates fake long-running process and records state;
- cleanup is idempotent;
- rerun creates a fresh registered run linked to previous run;
- no Pi/tmux/docker command assumptions appear in spawned-Hermes code path.

### #1379 — fake full workflow

End-to-end fake-Hermes tests:

- coder registration -> fake process -> artifact -> Den completion -> review request;
- reviewer registration -> fake process -> findings/verdict -> Den completion;
- fail-closed gates for registration failure, artifact missing, artifact identity mismatch, git mismatch, review request failure, reviewer failure, completion rejection;
- workflow result includes durable Den handles.

### #1380 — constrained live smoke

Live smoke checklist:

- disposable task or no-op child task only;
- narrow toolsets;
- short timeout;
- no secret echoing;
- registration visible in Den before process launch;
- worker writes deterministic artifact;
- completion packet accepted and retrievable;
- cleanup/retention documented.

### #1381 — rollout docs

Docs validation:

- explain when to use `delegate_task`, spawned-Hermes, or Pi;
- include operator commands/tool mapping;
- include failure signatures and recovery steps;
- include security/retention policy;
- link this contract and implementation tests.

## 11. Open design choices for follow-up tasks

1. Whether the Den registration API should be generic (`register_worker_run`) or substrate-specific (`register_spawned_hermes_worker_run`). Generic is preferable if validation remains strict.
2. Whether `session_id` should be allocated by Den at registration or updated after Hermes reveals a session ID. Initial implementation can set `session_id=run_id` and later enrich it.
3. Whether runtime metadata updates should be one generalized update tool or folded into status/cleanup calls.
4. Whether rerun reuses launch metadata through Den or the bridge stores enough local state to reconstruct it. Den should be the durable source of truth.
5. How long local logs/artifacts are retained by default.

## 12. Implementation north star

The local spawned-Hermes substrate should feel like a Den worker runtime, not a shell script wrapper. The bridge may spawn a process, but Den owns the run identity and completion state. The bridge owns local execution and verification. Neither side should treat process exit, prose summaries, or unregistered run IDs as authoritative completion evidence.


## 13. Implemented rollout status — tasks #1374-#1380

The tracked `spawned_hermes` path has now been implemented and smoke-tested across the bridge/Core split.

Implemented server/facade behavior:

- `mcp_den_register_worker_run` registers a durable Den worker run with `substrate="spawned_hermes"` before a local Hermes process is launched.
- `mcp_den_post_worker_completion_packet` remains authoritative only for registered worker runs; unregistered synthetic IDs still fail closed as `missing_run` / `missing_worker_run`.
- `mcp_den_get_latest_worker_completion`, `mcp_den_get_worker_run`, and `mcp_den_get_worker_run_status` observe registered spawned-Hermes runs and reconciled completion packets.
- `abort_worker_run`, `cleanup_worker_run`, and `rerun_worker_run` are substrate-aware. Core does not claim to abort an active spawned-Hermes process when it has no local process handle; the bridge/local runner owns process termination.

Implemented bridge behavior:

- `DenMcpAdapter.register_worker_run(...)` fails closed if the MCP tool is missing, Den rejects registration, or Den returns a mismatched run ID.
- `run_den_coder_reviewer_workflow(...)` registers coder and reviewer runs before launch, posts completion packets only after artifact verification, and stops before the next gate if Den rejects registration or completion.
- `SpawnedHermesLifecycle` tracks best-effort local process handles while the parent bridge process is alive, combines Den status with local process state, aborts live local subprocesses, cleans local artifacts idempotently, and derives rerun configs from launch metadata without stale artifact/log/PID fields.

Live smoke evidence:

- Synthetic registered completion smoke: run `spawned-hermes-smoke-1377-20260513T105710Z`, completion message `#5797`, status observed as `completed`.
- Real Hermes oneshot smoke: run `live-hermes-smoke-1380-20260513T114902Z`, session `worker-744e11a4b3fc00bf`, completion message `#5805`, status observed as `runtime=completed`, `completion=posted_completed`.
- The real smoke required invoking the child process with an explicit named Hermes profile: `hermes --profile den-hermes-runner --oneshot ... --toolsets file`. The default profile selected DeepSeek and failed without `DEEPSEEK_API_KEY`; named profiles carry copied `.env` and `auth.json` credentials.

Current validation evidence:

- `den-hermes`: `python -m pytest -q` → 33 passed.
- `den-core`: `dotnet test -v minimal` → Core 463 passed and Server 103 passed when the lifecycle-control changes were implemented.

## 14. Operator rollout guidance

### Choosing a substrate

Use direct `delegate_task` when the work is short-lived, synchronous, and acceptable to lose if the parent turn is interrupted. It is best for research fan-out, code review helpers, and context-efficient analysis. Because it is parent-turn-bound and summary-only, do not use it as the default durable Den coder/reviewer substrate.

Use `spawned_hermes` when Den needs a local Hermes worker with per-role profile/provider/model/toolsets, deterministic artifacts, logs, timeout/exit handling, abort/cleanup/rerun semantics, and authoritative completion through `post_worker_completion_packet`.

Use Pi/Docker workers when stronger sandboxing, server-managed isolation, or the existing Pi runtime behaviors are specifically required. Pi remains useful for environments where local profile credentials should not be exposed to a broad local process profile, or where Docker isolation is a hard requirement.

### Required launch sequence

1. Prepare or reference a bounded Den context packet when possible.
2. Compute deterministic per-run `artifact` and `log` paths.
3. Call `register_worker_run` with `substrate="spawned_hermes"`, role, profile/provider/model/toolsets, workdir, branch/base/head metadata, timeout, and artifact/log handles.
4. Spawn Hermes only after registration is accepted.
5. Invoke child Hermes with the explicit named profile that owns the required `.env`/`auth.json`, for example `--profile den-hermes-runner`; do not rely on the default profile.
6. Require the child to write a structured JSON artifact.
7. Verify artifact identity (`task_id`, `run_id`, `role`) and role-specific fields.
8. For coder artifacts, verify branch/head evidence before requesting review.
9. Post `post_worker_completion_packet` for the tracked run and fail closed if Den rejects it.
10. Advance review/finding/verdict state only after Den status/latest-completion observes the expected packet.

### Failure signatures

- `missing_run` / `missing_worker_run`: completion was posted for an unregistered run. Register first; do not treat this as success.
- provider configuration failure before artifact creation: the child Hermes CLI likely used the wrong profile or lacks `.env` / `auth.json`; rerun with an explicit `--profile` and/or configured provider/model.
- process exit 0 but no artifact: mark `incomplete` or post a failure packet; prose stdout is not a completion receipt.
- artifact identity mismatch: post/record failure, do not request review.
- Den completion rejection after artifact verification: fail closed and stop the workflow before reviewer/validator registration.
- active spawned-Hermes abort through Core returns blocked/no local process handle: terminate from the bridge process that owns the subprocess handle, then reconcile Den with a failure/abort packet.

### Artifact, log, cleanup, rerun, and retention

Artifacts and logs should be per-run and should include the run ID in the path. Keep them through at least the review/debug window; destructive local deletion should be explicit. `cleanup_worker_run` is idempotent for terminal Den records, but local artifact retention is an operator policy rather than automatic proof of success.

Rerun should allocate/register a fresh run ID linked to the prior run. Do not overwrite old logs or artifacts. Copy launch metadata such as profile/provider/model/toolsets/workdir/branch/base/head, but drop stale artifact paths, log paths, PIDs, and process/session handles.

### Credential and safety boundary

Den metadata may store profile names, provider names, model names, toolsets, and paths. It must not store `.env`, `auth.json`, raw API keys, OAuth tokens, provider credential pools, or complete environment dumps. Workers should receive the minimum toolsets needed for the role; `file` may be enough for a smoke, while coder/reviewer roles usually need more carefully scoped `terminal`/`file`/Den access.

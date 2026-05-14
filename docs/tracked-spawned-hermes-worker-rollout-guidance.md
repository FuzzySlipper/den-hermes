# Tracked spawned-Hermes worker rollout guidance

Task: `den-hermes-bridge` #1381  
Related umbrella: #1372  
Related exploration: #1368

## Summary

`spawned_hermes` is the Hermes-native local worker substrate for Den workflows that need durable worker-run identity without the Pi/tmux/docker runtime. A spawned Hermes subprocess is not authoritative by itself. Den is authoritative for the worker run, lifecycle state, and completion packet reconciliation.

The core rule is:

> Register the Den worker run before launching Hermes, and post `post_worker_completion_packet` only for that tracked run.

If Den reports `missing_run`, the workflow failed. Do not normalize it manually and do not treat process output or a child summary as success.

## When to use each substrate

### Direct `delegate_task`

Use direct Hermes `delegate_task` for short-lived synchronous helper work:

- research fan-out;
- review/checklist helpers;
- context pruning or librarian-style retrieval;
- quick code inspection that can be cancelled with the parent turn.

Do not use raw `delegate_task` as the default durable Den coder/reviewer worker runtime. It is parent-turn-bound and summary-only: if the parent is interrupted, the child is cancelled, and only a final summary returns to the parent.

### `spawned_hermes`

Use `spawned_hermes` when Den needs:

- first-class registered worker run identity;
- per-role Hermes profile/provider/model/toolsets;
- process timeout/exit handling;
- deterministic artifact and log paths;
- Den-observable status, latest completion, cleanup, rerun, and abort semantics;
- fail-closed coder→reviewer gating.

This is the preferred local Hermes replacement for Pi when Docker isolation is not required.

### Pi/Docker workers

Use Pi when the workflow specifically needs Docker isolation, server-managed runtime isolation, or existing Pi worker prompt/runtime behavior. Pi remains valid; spawned-Hermes is a lighter local substrate, not a blanket replacement for all security/sandboxing use cases.

## Required registration and launch sequence

1. Prepare a bounded context packet or state-file reference when possible.
2. Choose role, profile, provider, model, toolsets, timeout, workdir, branch/base/head guidance, artifact path, and log path.
3. Call `mcp_den_register_worker_run` with `substrate="spawned_hermes"`.
4. Fail closed if registration is missing, rejected, mismatched, or returns an unexpected run ID.
5. Launch the Hermes subprocess only after registration succeeds.
6. Invoke Hermes with the explicit named profile that has the expected `.env` / `auth.json`, for example:

```bash
hermes --profile den-hermes-runner --oneshot "$PROMPT" --toolsets file
```

7. Require a deterministic JSON artifact.
8. Verify artifact identity and role-specific fields.
9. Post `mcp_den_post_worker_completion_packet` for the registered run.
10. Verify with `mcp_den_get_latest_worker_completion` and `mcp_den_get_worker_run_status` before advancing the workflow.

## Completion packet rules

Use `post_worker_completion_packet` only after Den knows the run:

- coder → `implementation_packet`;
- reviewer → `review_findings_packet`;
- validator → `validation_packet`;
- drift checker → `drift_check_packet`;
- packet auditor → `packet_audit_packet`;
- infrastructure or subprocess failure → `worker_failure_packet`.

The packet must carry literal `run_id`/`session_id` identity from the registered run. Shell expressions, placeholders, stale run IDs, or child-generated guesses are invalid.

## Artifact and log paths

Every run should have deterministic per-run paths, e.g.:

```text
/tmp/den-hermes/<run_id>/completion.json
/tmp/den-hermes/<run_id>/worker.log
```

Artifacts should include at least:

```json
{
  "task_id": 1384,
  "run_id": "live-hermes-smoke-...",
  "role": "coder",
  "status": "completed",
  "summary": "safe summary"
}
```

Coder artifacts also need branch/head/tests evidence. Reviewer artifacts need verdict/findings evidence. The bridge verifies this before posting Den completion or advancing to review/finding state.

## Status, abort, cleanup, and rerun

### Status

Status combines Den state and local bridge state:

- Den worker run state;
- latest completion packet state;
- local process state when the bridge still has a process handle;
- artifact/log handles;
- diagnostics and cleanup eligibility.

### Abort

The bridge process that owns the subprocess handle is responsible for terminating a live spawned-Hermes process. Core cannot reliably kill a local subprocess if it only has a durable run record and no process handle. If server-side `abort_worker_run` reports that no local process handle exists, terminate from the bridge if possible, then reconcile Den with a failure/abort packet.

### Cleanup

Cleanup is idempotent for terminal Den records. Keep local artifacts/logs through at least the review/debug window. Deleting local files is an operator retention decision; Den's completion packet remains the authoritative lifecycle receipt.

### Rerun

Rerun registers a fresh run ID linked to the prior run. Reuse safe launch metadata such as profile/provider/model/toolsets/workdir/branch/base/head. Do not reuse stale artifact paths, log paths, PIDs, process handles, or session handles.

## Failure signatures and recovery

| Signature | Meaning | Recovery |
| --- | --- | --- |
| `missing_run` / `missing_worker_run` | Completion posted for unregistered run. | Register first; treat current attempt as failed. |
| Provider key/config error before artifact | Child used wrong Hermes profile or missing credentials. | Invoke with explicit `--profile`; ensure profile has `.env` and `auth.json`. |
| Exit 0 but missing artifact | Prose output is not a receipt. | Post/record failure or incomplete; do not advance. |
| Artifact identity mismatch | Wrong task/run/role evidence. | Fail closed; inspect prompt/env/path wiring. |
| Git branch/head mismatch | Coder evidence not verified. | Post coder failure; do not request review. |
| Completion packet rejected | Den did not accept authoritative receipt. | Stop before reviewer/validator; record diagnostic. |
| Server abort blocked/no handle | Core lacks local subprocess handle. | Abort in bridge process if alive, then reconcile Den. |

## Credential and safety boundaries

Den metadata may contain profile names, provider names, model names, toolsets, workdir, artifact/log paths, and branch/commit metadata. Den metadata must not contain `.env`, `auth.json`, API keys, OAuth tokens, provider credential pools, or full environment dumps.

Because this deployment currently copies shared `.env` and `auth.json` into Hermes profiles, be explicit about profile selection. The profile name is safe to record; the credential contents are not.

Use narrow toolsets whenever possible. The real smoke used only `file`. Coder/reviewer roles may need `terminal`, `file`, and Den access, but this should be role-scoped rather than blindly giving every spawned worker every tool.

## Live smoke evidence

### Full orchestrator smoke (#1401)

See `docs/spawned-hermes-orchestrator-rollout-1402.md` for the current coder → reviewer orchestrator workflow and operator checklist.

Successful Den-driven spawned-Hermes orchestrator smoke:

- task: #1401
- coder run: `live-smoke-coder-01e67fad`
- coder session: `worker-25466cb2b2b68a6b`
- coder branch/head: `task/1401-live-smoke` @ `ea7d0bc3dca68b67b177d2c0edfd0476768eb7da`
- coder artifact: `/tmp/den-hermes/live-smoke-coder-01e67fad/completion.json`
- coder completion message: #5866 (`implementation_packet`, completed)
- review round: #676
- reviewer run: `live-smoke-reviewer-6c40827a`
- reviewer session: `worker-4a139c6285a7f21f`
- reviewer artifact: `/tmp/den-hermes/live-smoke-reviewer-6c40827a/completion.json`
- reviewer completion message: #5871 (`review_findings_packet`, completed)
- verdict: `looks_good`, findings: none

Pre-smoke verification:

- `python -m pytest -q` → 106 passed
- `python -m den_hermes.runtime_ops preflight --roles coder,reviewer` → both roles passed

Important live-smoke lesson: pass a full `base_commit` to `request_review` / reviewer path. An empty base commit produced a generic Den MCP `request_review` invocation error; after adding robust MCP diagnostics and using the full base SHA, the reviewer path completed.

### Earlier single-worker smoke (#1384)

Successful real spawned-Hermes smoke:

- task: #1384
- run: `live-hermes-smoke-1380-20260513T114902Z`
- session: `worker-744e11a4b3fc00bf`
- artifact: `/tmp/den-hermes/live-hermes-smoke-1380-20260513T114902Z/completion.json`
- completion message: #5805
- status: `runtime=completed`, `completion=posted_completed`

Earlier failed attempt:

- run: `live-hermes-smoke-1380-20260513T111553Z`
- failure: default profile selected DeepSeek without `DEEPSEEK_API_KEY`
- resolution: rerun with explicit `--profile den-hermes-runner`

## Operator checklist

Before launching a real worker:

- [ ] task is disposable or scope-approved;
- [ ] repo/worktree is clean or expected;
- [ ] tests relevant to the bridge pass;
- [ ] Den registration call succeeds;
- [ ] Hermes subprocess uses the intended profile;
- [ ] artifact/log paths include run ID;
- [ ] artifact contract is in the prompt;
- [ ] completion packet is posted only after artifact verification;
- [ ] Den status/latest-completion observes the expected packet;
- [ ] logs/artifacts are retained or cleanup policy is explicit.

# Spawned-Hermes orchestrator rollout and operator workflow

Task: `den-hermes-bridge` #1402  
Related umbrella: #1394  
Status: rollout guidance after the constrained live smoke in #1401

## Summary

The first Den-driven `spawned_hermes` orchestrator workflow is now implemented and live-smoked for the coder → reviewer path. The orchestrator is a local Hermes-native bridge that reads Den workflow state, resolves role runtimes from the central registry, registers Den worker runs before launch, verifies local artifacts, posts tracked completion packets, and advances review state fail-closed.

This is the preferred local Hermes substrate when we need durable Den worker identity without Pi/Docker isolation. It does **not** replace direct `delegate_task` for short synchronous helper work, and it does **not** replace Pi/Docker workers when Docker/server isolation is required.

## Implemented workflow surface

Implemented child tasks under #1394:

| Task | Result |
| --- | --- |
| #1395 | Orchestrator state-machine skeleton |
| #1396 | Tracked coder path |
| #1397 | Tracked reviewer path |
| #1398 | Review-finding retry loop and stale-state guard |
| #1399 | Validator, drift-checker, and packet-auditor gate role paths |
| #1400 | Deterministic fake end-to-end test suite |
| #1409 | Live Den MCP HTTP adapter for the CLI/runner entrypoint |
| #1410 | Robust MCP response parsing and request-review diagnostics |
| #1401 | Constrained live coder → reviewer smoke |
| #1402 | This rollout/operator documentation |

## Runtime registry and preflight

The active operator-owned runtime registry is:

```text
/home/agents/runtime/spawned-hermes-runtimes.yaml
```

Current live matrix at #1402:

| Role | Profile | Provider | Model | Toolsets | Timeout | Runtime |
| --- | --- | --- | --- | --- | --- | --- |
| coder | `den-hermes-runner` | `openai-codex` | `gpt-5.5` | `terminal,file` | 1800s | `coder-primary` |
| reviewer | `den-hermes-runner` | `openai-codex` | `gpt-5.5` | `terminal,file` | 1500s | `reviewer-primary` |
| validator | `den-hermes-runner` | `openai-codex` | `gpt-5.5` | `terminal,file` | 1200s | `validator-primary` |
| drift_checker | `den-hermes-runner` | `openai-codex` | `gpt-5.5` | `terminal,file` | 900s | `drift-checker-primary` |
| packet_auditor | `den-hermes-runner` | `openai-codex` | `gpt-5.5` | `terminal,file` | 900s | `packet-auditor-primary` |

Preflight before live work:

```bash
python -m den_hermes.runtime_ops validate
python -m den_hermes.runtime_ops matrix
python -m den_hermes.runtime_ops preflight --roles coder,reviewer
python -m pytest -q
```

`DEN_HERMES_RUNTIME_REGISTRY` may override the registry path for tests/emergency use, but production operator runs should use the shared registry above.

## CLI/API usage

### Evaluate next Den action

The CLI evaluates Den workflow state through the live MCP HTTP adapter. It requires an explicit Den MCP endpoint; there is no hard-coded default endpoint.

```bash
DEN_HERMES_MCP_URL=http://192.168.1.10:5199/mcp \
DEN_HERMES_MCP_TIMEOUT=30 \
python -m den_hermes.orchestrator \
  --project-id den-hermes-bridge \
  --task-id <task-id> \
  --json
```

Current CLI scope: action evaluation / adapter smoke. Role-path launches are exposed as Python API helpers and are invoked by runner code until a full operator CLI command is added.

### Python role-path API

Common pattern:

```python
from pathlib import Path
from den_hermes.orchestrator import (
    build_mcp_adapter,
    run_tracked_coder_path,
    run_tracked_reviewer_path,
)

adapter = build_mcp_adapter(
    project_id="den-hermes-bridge",
    requested_by="den-hermes-runner",
)

coder = run_tracked_coder_path(
    adapter,
    task_id=TASK_ID,
    prompt=bounded_coder_prompt,
    run_id="stable-run-id",
    cwd=Path("/home/dev/den-hermes"),
    runtime_registry_path="/home/agents/runtime/spawned-hermes-runtimes.yaml",
    verify_git=True,
    branch="task/<id>-branch",
    base_branch="main-or-parent-branch",
    base_commit="<full-base-sha>",
)

reviewer = run_tracked_reviewer_path(
    adapter,
    task_id=TASK_ID,
    prompt=bounded_reviewer_prompt,
    run_id="stable-reviewer-run-id",
    coder_artifact={
        "run_id": coder.run_id,
        "branch": coder.branch,
        "head_commit": coder.head_commit,
        "tests_run": [{"command": "pytest -q", "result": "passed"}],
    },
    cwd=Path("/home/dev/den-hermes"),
    runtime_registry_path="/home/agents/runtime/spawned-hermes-runtimes.yaml",
    base_branch="main-or-parent-branch",
    base_commit="<full-base-sha>",
)
```

Important: provide a real full `base_commit` for reviewer review requests. The live smoke showed that empty base commits can surface as a generic Den `request_review` invocation error. With a full base commit, the review request, reviewer launch, completion packet, review findings packet, and verdict all reconciled correctly.

## State-machine behavior

The orchestrator reads Den task workflow summary and chooses fail-closed actions:

- planned/in-progress task without implementation evidence → launch coder path;
- completed coder evidence with branch/head/tests → request review and launch reviewer path;
- review verdict `changes_requested` with open findings → launch coder retry if the review state is still current and retry budget remains;
- verdict `looks_good` → done-ready / no retry;
- follow-up-only findings → defer to follow-up handling;
- blocked/done/cancelled state → hold;
- missing, malformed, stale, or rejected Den packets → block/fail instead of advancing.

Stale-state guard: before retrying after review findings, the orchestrator re-reads task workflow state and refuses to act if a newer review round exists or the task reached a terminal/manual approval state.

## Role artifact and packet mapping

| Role | Required artifact evidence | Den completion packet |
| --- | --- | --- |
| coder | branch, full head commit, tests run | `implementation_packet` |
| reviewer | verdict, findings, optional tests run | `review_findings_packet` |
| validator | passing verdict plus tests/validation evidence | `validation_packet` |
| drift_checker | passing verdict plus checked refs/packets | `drift_check_packet` |
| packet_auditor | passing verdict plus audited packet refs | `packet_audit_packet` |

Gate roles (`validator`, `drift_checker`, `packet_auditor`) only accept `passed`, `pass`, `looks_good`, or `ok` verdicts for successful artifacts. Non-passing gate verdicts are failed before Den completion is posted.

## Live smoke evidence (#1401)

Preflight:

```text
python -m pytest -q => 106 passed
python -m den_hermes.runtime_ops preflight --roles coder,reviewer => OK
```

Coder path:

- run: `live-smoke-coder-01e67fad`
- session: `worker-25466cb2b2b68a6b`
- branch: `task/1401-live-smoke`
- head: `ea7d0bc3dca68b67b177d2c0edfd0476768eb7da`
- artifact: `/tmp/den-hermes/live-smoke-coder-01e67fad/completion.json`
- completion packet: Den message #5866, `implementation_packet`, `completed`
- worker status: `runtime=completed`, `completion=posted_completed`
- focused test: `python -m pytest tests/test_orchestrator_mcp_wiring.py -q` => 5 passed

Reviewer path:

- review round: #676
- reviewer run: `live-smoke-reviewer-6c40827a`
- session: `worker-4a139c6285a7f21f`
- base branch: `task/1409-real-den-mcp-adapter`
- base commit: `fbd504f88670d3af525a457003a329fc65e2d379`
- reviewed head: `ea7d0bc3dca68b67b177d2c0edfd0476768eb7da`
- artifact: `/tmp/den-hermes/live-smoke-reviewer-6c40827a/completion.json`
- completion packet: Den message #5871, `review_findings_packet`, `completed`
- verdict: `looks_good`
- findings: none
- reviewer verified only `docs/live-smoke-1401.md` changed and no secrets were present.

## Failure signatures and recovery

| Signature | Meaning | Recovery |
| --- | --- | --- |
| `No Den MCP tools object was injected` | Old CLI had no live MCP adapter | Use #1409+ code with explicit `DEN_HERMES_MCP_URL`. |
| `DEN_HERMES_MCP_URL or DEN_MCP_URL must be set` | Live adapter intentionally has no endpoint default | Set an explicit MCP URL in the operator environment. |
| `MCP tool request_review returned non-JSON text content: "An error occurred invoking 'request_review'."` | Den MCP facade returned a generic tool error, observed when `base_commit` was empty | Retry with a real full base commit; if still failing, inspect Den Core/MCP logs and create a Den API bug with tool args redacted. |
| `MCP initialize response missing Mcp-Session-Id header` | MCP session failed to initialize correctly | Check Den MCP service health and transport compatibility. |
| completion `missing_run` / `missing_worker_run` | Completion packet posted for unregistered run | Treat attempt as failed; register the worker first and rerun. |
| exit 0 but no artifact | Worker prose is not an authoritative receipt | Post failure/incomplete; do not advance to review. |
| branch/head mismatch | Coder artifact cannot be verified against git | Block before review and inspect worktree/branch guidance. |
| stale review state | A newer review round or terminal verdict exists | Re-read Den state and stop; do not launch retry from stale findings. |

## Artifact, log retention, cleanup, abort, and rerun

- Artifact path convention: `/tmp/den-hermes/<run_id>/completion.json`.
- Log path convention: `/tmp/den-hermes/<run_id>/worker.log`.
- Keep artifacts/logs through at least the review/debug window. Den completion packets remain the durable authoritative receipt.
- Use `mcp_den_get_worker_run_status` and `mcp_den_get_latest_worker_completion` for status; tmux/process attach is break-glass observability only.
- Cleanup is idempotent for terminal runs; use `cleanup_worker_run` after preserving needed forensics.
- Abort/rerun should create a clear Den record. A rerun should use a fresh run id and fresh artifact/log paths, not reuse stale handles.

## Choosing delegate_task vs spawned-Hermes vs Pi

Use direct `delegate_task` when:

- the work is synchronous and parent-turn-bound;
- a final summary is sufficient;
- cancellation with the parent turn is acceptable;
- there is no need for Den worker lifecycle, durable completion packets, or retry/cleanup controls.

Use `spawned_hermes` orchestrator when:

- Den needs first-class worker-run identity;
- coder/reviewer/gate roles must be visible in Den;
- branch/head/tests/finding evidence must be reconciled before advancing;
- local Hermes profiles are enough and Docker isolation is not required.

Use Pi/Docker workers when:

- Docker/container isolation or server-managed runtime behavior is required;
- existing Pi prompts/runtime semantics are the target under test;
- the work should run in the server-side Pi substrate rather than a local Hermes subprocess.

## Operator checklist

Before a real spawned-Hermes orchestrator run:

- [ ] Den task scope is disposable or approved.
- [ ] Repo/worktree state is understood and branch/base/head are explicit.
- [ ] `python -m pytest -q` passes or failures are intentionally scoped.
- [ ] `python -m den_hermes.runtime_ops preflight --roles <roles>` passes.
- [ ] `DEN_HERMES_MCP_URL` / `DEN_MCP_URL` is explicit.
- [ ] Run IDs, artifact paths, and log paths include the role/run id.
- [ ] Coder prompt requires writing the JSON artifact to `DEN_EXPECTED_ARTIFACT`.
- [ ] Coder completion is verified in Den before requesting review.
- [ ] Reviewer request includes a full base commit.
- [ ] Reviewer completion and review verdict are verified in Den before marking done.
- [ ] Blockers create follow-up Den tasks with exact run IDs/status/error evidence.

## Current follow-ups

No unresolved blocker remains for the constrained coder → reviewer live smoke. The remaining improvements are operational hardening rather than blockers:

- add first-class CLI subcommands for launching coder/reviewer/gate paths instead of requiring Python snippets;
- decide whether and when to merge/publish smoke or implementation branches to the target integration branch;
- extend live smoke coverage to validator, drift_checker, and packet_auditor gates;
- add Den Core/MCP schema fields for runtime registry audit metadata (`runtime_id`, registry fingerprint) when ready.

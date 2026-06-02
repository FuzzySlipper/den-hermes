# ADR: Target-vs-Runtime Project Attribution in Hermes Bridge

**Task**: #1847
**Status**: Active
**Date**: 2026-06-02
**Cross-references**: den-core/worker-target-runtime-attribution-contract, #1844, #1845, #1846, #1842

## Context

Hermes Bridge is the runtime adapter/bus depot for spawned-Hermes workers. Workers can be launched through a shared control channel owned by `den-hermes-bridge` while doing work for a different target project (e.g., `goblinbench`). The GoblinBench pooled-orchestrator retry exposed a boundary leak: worker transport/control activity appeared as `den-hermes-bridge` even while implementation/review/validation packets correctly belonged to the target project.

## Decision

Introduce a clear target-vs-runtime project distinction in the Bridge code:

1. **`project_id`** (runtime/control project): The bridge/adapter project. Used for pool-level queries: leases, assignments, residency, infrastructure notifications.

2. **`target_project_id`** (work-owning project): The project owning the work being done. Used for task-scoped operations: completion packets, worker runs, context packets, review requests, status messages.

When `target_project_id` is unset (None), all operations fall back to `project_id` for backward compatibility.

## Implementation

### DenWorkflowAdapter (orchestrator.py)

Added `target_project_id: str | None = None` field and `work_project_id` property. Task-scoped MCP methods use `work_project_id` (which returns `target_project_id or project_id`). Pool/lease methods continue using `project_id`.

### worker_launcher.py

Added `DEN_TARGET_PROJECT_ID` environment variable for spawned workers. Workers can now distinguish the target project from the runtime project.

### channels_bridge.py

Enhanced `_reply_metadata` to carry explicit `target_project_id` and `runtime_project_id` in response metadata. The bridge sender identity remains correct (it IS the bridge), but metadata now clearly separates target work attribution from runtime transport attribution.

### Activity context

Enhanced `_child_activity_context` to propagate `targetProjectId` and `runtimeProjectId` to child workers through the activity context chain.

### Status/readback

`enrich_final_status` now includes `target_project_id` and `runtime_project_id` when they differ.

## Invariants

1. Target project/task/assignment/run are workflow attribution and must not be inferred only from channel project.
2. Runtime/control project is transport attribution and must not imply ownership of target work.
3. Completion packets belong in the target project/task thread.
4. Bridge may log/report profile/session details, but user-facing work evidence remains Den-facing: worker role, assignment, run, target project/task.

## Non-goal

Do not move Core workflow state into Bridge. Bridge remains an adapter over Den contracts.

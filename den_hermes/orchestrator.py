from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping


class OrchestratorActionType(str, Enum):
    START_CODER = "start_coder"
    AWAIT_CODER = "await_coder"
    START_REVIEWER = "start_reviewer"
    AWAIT_REVIEWER = "await_reviewer"
    HANDLE_CHANGES_REQUESTED = "handle_changes_requested"
    DONE = "done"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True)
class OrchestratorAction:
    type: OrchestratorActionType
    reason: str
    role: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["type"] = self.type.value
        return payload


@dataclass(frozen=True)
class DenWorkflowAdapter:
    """Small Den workflow-state adapter for the orchestrator skeleton.

    The MCP tools object is injected so unit tests can provide fakes and later
    tasks can wire the real Hermes/Den MCP tool surface without baking globals
    into the state machine.
    """

    tools: Any
    project_id: str
    requested_by: str

    def get_task_workflow_summary(self, *, task_id: int) -> Mapping[str, Any]:
        response = self.tools.mcp_den_get_task_workflow_summary(task_id=task_id)
        return _coerce_mapping_response(response)

    def determine_orchestrator_next_action(self, *, task_id: int, max_attempts: int = 3) -> Mapping[str, Any]:
        response = self.tools.mcp_den_determine_orchestrator_next_action(
            project_id=self.project_id,
            task_id=task_id,
            max_attempts=max_attempts,
        )
        return _coerce_mapping_response(response)

    def get_latest_worker_completion(self, *, task_id: int, run_id: str, role: str | None = None) -> Mapping[str, Any]:
        args: dict[str, Any] = {"project_id": self.project_id, "task_id": task_id, "run_id": run_id}
        if role is not None:
            args["role"] = role
        response = self.tools.mcp_den_get_latest_worker_completion(**args)
        return _coerce_mapping_response(response)


def decide_next_action(adapter: DenWorkflowAdapter, *, task_id: int, max_attempts: int = 3) -> OrchestratorAction:
    """Read Den workflow state and return the next orchestrator action.

    This task intentionally does not launch workers. It only normalizes Den's
    workflow summary / next-action decision into bridge-local action categories
    that later tasks can attach to coder/reviewer launch paths.
    """

    summary = adapter.get_task_workflow_summary(task_id=task_id)
    decision = adapter.determine_orchestrator_next_action(task_id=task_id, max_attempts=max_attempts)
    task_status = _task_status(summary)
    raw_action = _raw_next_action(decision, summary=summary, task_status=task_status)
    action_type = _normalize_action_type(raw_action, task_status=task_status)
    reason = str(decision.get("reason") or decision.get("summary") or _default_reason(action_type))
    details = _action_details(decision, summary=summary, task_status=task_status)
    return OrchestratorAction(
        type=action_type,
        reason=reason,
        role=_role_for_action(action_type),
        details=details,
    )


def build_mcp_adapter(*, project_id: str, requested_by: str) -> DenWorkflowAdapter:
    """Build a real MCP-backed adapter.

    Hermes exposes MCP tools to the agent process, not automatically to arbitrary
    Python subprocesses. Future wiring can pass a concrete tools object here;
    for now this explicit error keeps the module CLI fail-closed instead of
    silently pretending it can reach Den.
    """

    raise RuntimeError(
        "No Den MCP tools object was injected. Use decide_next_action() with an injected "
        "DenWorkflowAdapter from Hermes, or wire build_mcp_adapter in a future integration task."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the next Den spawned-Hermes orchestrator action.")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--task-id", type=int, required=True)
    parser.add_argument("--requested-by", default="den-hermes-runner")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--json", action="store_true", help="Print a machine-readable JSON action")
    args = parser.parse_args(argv)

    adapter = build_mcp_adapter(project_id=args.project_id, requested_by=args.requested_by)
    action = decide_next_action(adapter, task_id=args.task_id, max_attempts=args.max_attempts)
    if args.json:
        print(json.dumps(action.to_json_dict(), sort_keys=True))
    else:
        role = f" role={action.role}" if action.role else ""
        print(f"{action.type.value}{role}: {action.reason}")
    return 0


def _coerce_mapping_response(response: Any) -> Mapping[str, Any]:
    if isinstance(response, str):
        parsed = json.loads(response)
        if isinstance(parsed, Mapping):
            return parsed
        raise TypeError(f"Expected mapping response, got {type(parsed).__name__}")
    if isinstance(response, Mapping) and isinstance(response.get("result"), str):
        parsed = json.loads(response["result"])
        if isinstance(parsed, Mapping):
            return parsed
    if isinstance(response, Mapping):
        return response
    raise TypeError(f"Expected mapping response, got {type(response).__name__}")


def _task_status(summary: Mapping[str, Any]) -> str | None:
    task = summary.get("task")
    if isinstance(task, Mapping) and task.get("status") is not None:
        return str(task["status"])
    if summary.get("status") is not None:
        return str(summary["status"])
    return None


def _raw_next_action(decision: Mapping[str, Any], *, summary: Mapping[str, Any], task_status: str | None) -> str:
    for key in ("next_action", "action", "recommended_action", "state"):
        value = decision.get(key)
        if value:
            return str(value)
    review_state = summary.get("current_review_state")
    if isinstance(review_state, Mapping) and review_state.get("verdict") == "changes_requested":
        return "handle_changes_requested"
    if task_status in {"done", "cancelled"}:
        return "done"
    if task_status == "blocked":
        return "blocked"
    return "failed"


def _normalize_action_type(raw_action: str, *, task_status: str | None) -> OrchestratorActionType:
    normalized = raw_action.strip().lower().replace("-", "_").replace(" ", "_")
    if task_status in {"done", "cancelled"} and normalized in {"done", "noop", "no_op", "complete", "completed"}:
        return OrchestratorActionType.DONE
    mapping = {
        "start_coder": OrchestratorActionType.START_CODER,
        "launch_coder": OrchestratorActionType.START_CODER,
        "coder_needed": OrchestratorActionType.START_CODER,
        "run_coder": OrchestratorActionType.START_CODER,
        "await_coder": OrchestratorActionType.AWAIT_CODER,
        "wait_for_coder": OrchestratorActionType.AWAIT_CODER,
        "coder_running": OrchestratorActionType.AWAIT_CODER,
        "request_review": OrchestratorActionType.START_REVIEWER,
        "start_review": OrchestratorActionType.START_REVIEWER,
        "start_reviewer": OrchestratorActionType.START_REVIEWER,
        "launch_reviewer": OrchestratorActionType.START_REVIEWER,
        "reviewer_needed": OrchestratorActionType.START_REVIEWER,
        "await_reviewer": OrchestratorActionType.AWAIT_REVIEWER,
        "wait_for_reviewer": OrchestratorActionType.AWAIT_REVIEWER,
        "reviewer_running": OrchestratorActionType.AWAIT_REVIEWER,
        "changes_requested": OrchestratorActionType.HANDLE_CHANGES_REQUESTED,
        "handle_changes_requested": OrchestratorActionType.HANDLE_CHANGES_REQUESTED,
        "retry_coder": OrchestratorActionType.HANDLE_CHANGES_REQUESTED,
        "done": OrchestratorActionType.DONE,
        "noop": OrchestratorActionType.DONE,
        "no_op": OrchestratorActionType.DONE,
        "complete": OrchestratorActionType.DONE,
        "completed": OrchestratorActionType.DONE,
        "blocked": OrchestratorActionType.BLOCKED,
        "needs_input": OrchestratorActionType.BLOCKED,
        "escalate": OrchestratorActionType.BLOCKED,
        "blocked_by_dependency": OrchestratorActionType.BLOCKED,
        "failed": OrchestratorActionType.FAILED,
        "failure": OrchestratorActionType.FAILED,
    }
    if normalized in mapping:
        return mapping[normalized]
    if task_status == "blocked":
        return OrchestratorActionType.BLOCKED
    return OrchestratorActionType.FAILED


def _role_for_action(action_type: OrchestratorActionType) -> str | None:
    if action_type in {OrchestratorActionType.START_CODER, OrchestratorActionType.AWAIT_CODER}:
        return "coder"
    if action_type in {OrchestratorActionType.START_REVIEWER, OrchestratorActionType.AWAIT_REVIEWER}:
        return "reviewer"
    if action_type is OrchestratorActionType.HANDLE_CHANGES_REQUESTED:
        return "coder"
    return None


def _action_details(
    decision: Mapping[str, Any],
    *,
    summary: Mapping[str, Any],
    task_status: str | None,
) -> dict[str, Any]:
    details: dict[str, Any] = {"task_status": task_status}
    for key in (
        "run_id",
        "coder_run_id",
        "reviewer_run_id",
        "review_round_id",
        "finding_ids",
        "max_attempts",
        "attempt",
        "failure_category",
    ):
        if key in decision:
            details[key] = decision[key]
    if "unresolved_findings" in summary and "finding_ids" not in details:
        findings = summary.get("unresolved_findings")
        if isinstance(findings, list):
            details["finding_ids"] = [finding.get("id") for finding in findings if isinstance(finding, Mapping)]
    return details


def _default_reason(action_type: OrchestratorActionType) -> str:
    return {
        OrchestratorActionType.START_CODER: "Den workflow state requires a coder worker.",
        OrchestratorActionType.AWAIT_CODER: "Coder worker is still pending completion.",
        OrchestratorActionType.START_REVIEWER: "Coder output is ready for reviewer workflow.",
        OrchestratorActionType.AWAIT_REVIEWER: "Reviewer worker is still pending completion.",
        OrchestratorActionType.HANDLE_CHANGES_REQUESTED: "Review findings require a coder retry path.",
        OrchestratorActionType.DONE: "Workflow is already terminal.",
        OrchestratorActionType.BLOCKED: "Workflow is blocked or needs input.",
        OrchestratorActionType.FAILED: "Workflow state could not be mapped to a safe action.",
    }[action_type]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

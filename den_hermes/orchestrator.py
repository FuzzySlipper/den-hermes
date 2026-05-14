from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from den_hermes.runtime_registry import DEFAULT_RUNTIME_REGISTRY_PATH, RuntimeRegistryError, resolve_role_runtime
from den_hermes.worker_launcher import run_hermes_worker, _verify_git_branch_head


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
class CoderPathResult:
    status: str
    run_id: str
    branch: str | None = None
    head_commit: str | None = None
    artifact_path: str | None = None
    latest_completion: Mapping[str, Any] | None = None
    worker_status: Mapping[str, Any] | None = None
    error: str | None = None


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

    def prepare_coder_context_packet(
        self,
        *,
        task_id: int,
        branch: str | None = None,
        base_branch: str | None = None,
        base_commit: str | None = None,
        allowed_scope: str | None = None,
        notes: str | None = None,
    ) -> Mapping[str, Any]:
        args: dict[str, Any] = {
            "project_id": self.project_id,
            "task_id": task_id,
            "requested_by": self.requested_by,
        }
        optional = {
            "branch": branch,
            "base_branch": base_branch,
            "base_commit": base_commit,
            "allowed_scope": allowed_scope,
            "notes": notes,
        }
        args.update({key: value for key, value in optional.items() if value is not None})
        return _coerce_mapping_response(self.tools.mcp_den_prepare_coder_context_packet(**args))

    def register_worker_run(self, **kwargs: Any) -> Mapping[str, Any]:
        normalized = dict(kwargs)
        normalized.pop("runtime_id", None)
        if "toolsets" in normalized:
            normalized["toolsets"] = _csv_or_none(normalized["toolsets"])
        args = {
            "project_id": self.project_id,
            "requested_by": self.requested_by,
            "substrate": "spawned_hermes",
            **normalized,
        }
        response = self.tools.mcp_den_register_worker_run(**args)
        payload = _coerce_mapping_response(response)
        _ensure_den_did_not_reject(payload, context=f"worker registration for {kwargs.get('run_id')}")
        return payload

    def mark_worker_started(self, *, task_id: int, run_id: str, role: str) -> Mapping[str, Any]:
        response = self.tools.mcp_den_send_message(
            project_id=self.project_id,
            sender=self.requested_by,
            task_id=task_id,
            content=f"Spawned-Hermes {role} worker `{run_id}` started by orchestrator.",
            metadata={"type": "spawned_hermes_orchestrator_worker_started", "run_id": run_id, "role": role},
            intent="handoff",
        )
        return _coerce_mapping_response(response)

    def mark_worker_completed(self, *, task_id: int, run_id: str, role: str, artifact: Mapping[str, Any]) -> Mapping[str, Any]:
        args: dict[str, Any] = {
            "project_id": self.project_id,
            "run_id": run_id,
            "requested_by": self.requested_by,
            "status": str(artifact.get("status", "completed")),
            "role": role,
            "packet_type": "implementation_packet" if role == "coder" else "worker_failure_packet",
            "summary": str(artifact.get("summary", "Spawned-Hermes worker completed.")),
            "dedupe_key": f"{run_id}:completed",
        }
        if role == "coder":
            args.update(
                {
                    "branch": artifact.get("branch"),
                    "head_commit": artifact.get("head_commit"),
                    "base_commit": artifact.get("base_commit"),
                    "tests_run": json.dumps(list(artifact.get("tests_run", []))),
                }
            )
        response = self.tools.mcp_den_post_worker_completion_packet(**args)
        payload = _coerce_mapping_response(response)
        _ensure_den_did_not_reject(payload, context=f"worker completion for {run_id}")
        return payload

    def mark_worker_failed(self, *, task_id: int, run_id: str, role: str, error: str) -> Mapping[str, Any]:
        response = self.tools.mcp_den_post_worker_completion_packet(
            project_id=self.project_id,
            run_id=run_id,
            requested_by=self.requested_by,
            status="failed",
            role=role,
            packet_type="worker_failure_packet",
            summary=error,
            failure_category="spawned_hermes_orchestrator_worker_failed",
            recovery_guidance="Inspect orchestrator state, spawned-Hermes logs, and completion artifact evidence before retry.",
            dedupe_key=f"{run_id}:failed",
        )
        return _coerce_mapping_response(response)

    def get_worker_run_status(self, *, task_id: int, run_id: str) -> Mapping[str, Any]:
        response = self.tools.mcp_den_get_worker_run_status(
            project_id=self.project_id,
            task_id=task_id,
            run_id=run_id,
        )
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


def run_tracked_coder_path(
    adapter: DenWorkflowAdapter,
    *,
    task_id: int,
    prompt: str,
    run_id: str,
    cwd: str | Path | None = None,
    env_overrides: Mapping[str, str] | None = None,
    runtime_registry_path: str | Path | None = None,
    verify_git: bool = True,
    branch: str | None = None,
    base_branch: str | None = None,
    base_commit: str | None = None,
    allowed_scope: str | None = None,
) -> CoderPathResult:
    """Run the tracked spawned-Hermes coder path for a START_CODER action.

    This is the first executable role path for the orchestrator. It prepares a
    bounded Den coder context packet, resolves the centralized coder runtime,
    registers the worker before launch, validates the artifact through the
    existing launcher, verifies git branch/head evidence, posts the authoritative
    implementation packet, and returns durable Den handles.
    """

    try:
        packet = adapter.prepare_coder_context_packet(
            task_id=task_id,
            branch=branch,
            base_branch=base_branch,
            base_commit=base_commit,
            allowed_scope=allowed_scope,
            notes="Prepared by spawned-Hermes orchestrator coder path.",
        )
        packet_message_id = _packet_message_id(packet)
        runtime = resolve_role_runtime(
            "coder",
            registry_path=_selected_runtime_registry_path(runtime_registry_path),
            run_id=run_id,
        )
    except (RuntimeRegistryError, KeyError, TypeError, ValueError) as exc:
        return CoderPathResult(status="failed", run_id=run_id, error=str(exc))

    artifact_path = runtime.artifact_path or str(Path(runtime.run_root) / run_id / runtime.artifact_filename)
    log_path = runtime.log_path or str(Path(runtime.run_root) / run_id / runtime.log_filename)
    try:
        adapter.register_worker_run(
            task_id=task_id,
            run_id=run_id,
            role="coder",
            branch=branch,
            base_branch=base_branch,
            base_commit=base_commit,
            profile=runtime.profile,
            provider=runtime.provider,
            model=runtime.model,
            toolsets=list(runtime.toolsets),
            workdir=str(cwd) if cwd is not None else runtime.workdir,
            timeout_seconds=runtime.timeout_seconds,
            artifact_path=artifact_path,
            log_path=log_path,
            runtime_id=runtime.runtime_id,
            prompt_packet_message_id=packet_message_id,
            dedupe_key=f"{task_id}:coder:{run_id}",
        )
    except Exception as exc:  # noqa: BLE001 - fail closed before subprocess launch
        return CoderPathResult(status="failed", run_id=run_id, artifact_path=artifact_path, error=str(exc))

    adapter.mark_worker_started(task_id=task_id, run_id=run_id, role="coder")
    worker_prompt = _coder_prompt_with_packet(prompt=prompt, packet_message_id=packet_message_id)
    worker = run_hermes_worker(
        task_id=task_id,
        run_id=run_id,
        role="coder",
        prompt=worker_prompt,
        expected_artifact=artifact_path,
        provider=runtime.provider,
        model=runtime.model,
        profile=runtime.profile,
        toolsets=list(runtime.toolsets),
        cwd=cwd if cwd is not None else runtime.workdir,
        env_overrides=env_overrides,
        timeout_seconds=runtime.timeout_seconds,
    )
    if worker.status != "completed" or worker.artifact is None:
        error = worker.error or "Coder worker did not complete"
        adapter.mark_worker_failed(task_id=task_id, run_id=run_id, role="coder", error=error)
        return CoderPathResult(status="failed", run_id=run_id, artifact_path=artifact_path, error=error)

    if verify_git:
        git_error = _verify_git_branch_head(worker.artifact, cwd=cwd if cwd is not None else runtime.workdir)
        if git_error:
            adapter.mark_worker_failed(task_id=task_id, run_id=run_id, role="coder", error=git_error)
            return CoderPathResult(
                status="failed",
                run_id=run_id,
                branch=str(worker.artifact.get("branch")),
                head_commit=str(worker.artifact.get("head_commit")),
                artifact_path=artifact_path,
                error=git_error,
            )

    try:
        adapter.mark_worker_completed(task_id=task_id, run_id=run_id, role="coder", artifact=worker.artifact)
    except Exception as exc:  # noqa: BLE001 - Den rejected authoritative completion
        return CoderPathResult(
            status="failed",
            run_id=run_id,
            branch=str(worker.artifact.get("branch")),
            head_commit=str(worker.artifact.get("head_commit")),
            artifact_path=artifact_path,
            error=f"Coder completion rejected by Den: {exc}",
        )
    latest_completion = adapter.get_latest_worker_completion(task_id=task_id, run_id=run_id, role="coder")
    worker_status = adapter.get_worker_run_status(task_id=task_id, run_id=run_id)
    return CoderPathResult(
        status="completed",
        run_id=run_id,
        branch=str(worker.artifact["branch"]),
        head_commit=str(worker.artifact["head_commit"]),
        artifact_path=artifact_path,
        latest_completion=latest_completion,
        worker_status=worker_status,
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


def _selected_runtime_registry_path(runtime_registry_path: str | Path | None) -> str | Path:
    return runtime_registry_path or os.environ.get("DEN_HERMES_RUNTIME_REGISTRY") or DEFAULT_RUNTIME_REGISTRY_PATH


def _packet_message_id(packet: Mapping[str, Any]) -> int:
    value = packet.get("message_id", packet.get("id"))
    if value is None:
        raise ValueError("coder context packet response must include message_id or id")
    return int(value)


def _coder_prompt_with_packet(*, prompt: str, packet_message_id: int) -> str:
    return (
        f"{prompt.rstrip()}\n\n"
        "DEN CODER CONTEXT PACKET\n"
        f"Use Den task-thread packet message id {packet_message_id} as the bounded coder context source.\n"
    )


def _ensure_den_did_not_reject(payload: Mapping[str, Any], *, context: str) -> None:
    completion_state = payload.get("completion_state")
    failure_category = payload.get("failure_category")
    if payload.get("error") or payload.get("status") == "error" or failure_category:
        raise RuntimeError(str(payload.get("summary") or payload.get("error") or f"Den rejected {context}"))
    if completion_state in {"missing_run", "malformed", "rejected"}:
        raise RuntimeError(str(payload.get("summary") or f"Den rejected {context}: {completion_state}"))


def _csv_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return ",".join(str(item) for item in value)


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

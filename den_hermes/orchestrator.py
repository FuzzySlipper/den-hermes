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
    claimed_finding_ids: list[int] = field(default_factory=list)
    response_notes: str | None = None


@dataclass(frozen=True)
class ReviewerPathResult:
    status: str
    run_id: str
    verdict: str | None = None
    finding_ids: list[int] = field(default_factory=list)
    artifact_path: str | None = None
    latest_completion: Mapping[str, Any] | None = None
    worker_status: Mapping[str, Any] | None = None
    review_request: Mapping[str, Any] | None = None
    error: str | None = None


@dataclass(frozen=True)
class ReviewLoopResult:
    status: str
    reason: str
    run_id: str | None = None
    finding_ids: list[int] = field(default_factory=list)
    coder_result: CoderPathResult | None = None


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

    def request_review(
        self,
        *,
        task_id: int,
        branch: str,
        head_commit: str,
        tests_run: Any,
        coder_run_id: str | None = None,
        base_branch: str | None = None,
        base_commit: str | None = None,
    ) -> Mapping[str, Any]:
        args: dict[str, Any] = {
            "project_id": self.project_id,
            "task_id": task_id,
            "requested_by": self.requested_by,
            "branch": branch,
            "base_branch": base_branch or "main",
            "base_commit": base_commit or "",
            "head_commit": head_commit,
            "tests_run": json.dumps(list(tests_run or [])),
        }
        if coder_run_id is not None:
            args["run_id"] = coder_run_id
        return _coerce_mapping_response(self.tools.mcp_den_request_review(**args))

    def prepare_reviewer_context_packet(
        self,
        *,
        task_id: int,
        review_round_id: int | None,
        branch: str,
        head_commit: str,
        base_branch: str | None = None,
        base_commit: str | None = None,
        notes: str | None = None,
    ) -> Mapping[str, Any]:
        args: dict[str, Any] = {
            "project_id": self.project_id,
            "task_id": task_id,
            "requested_by": self.requested_by,
            "branch": branch,
            "head_commit": head_commit,
        }
        optional = {
            "review_round_id": review_round_id,
            "base_branch": base_branch,
            "base_commit": base_commit,
            "notes": notes,
        }
        args.update({key: value for key, value in optional.items() if value is not None})
        return _coerce_mapping_response(self.tools.mcp_den_prepare_reviewer_context_packet(**args))

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
            "packet_type": _packet_type_for_role(role),
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
        if role == "reviewer":
            if "finding_ids" in artifact:
                args["finding_ids"] = json.dumps(list(artifact.get("finding_ids") or []))
            if "tests_run" in artifact:
                args["tests_run"] = json.dumps(list(artifact.get("tests_run") or []))
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

    def create_review_finding(
        self,
        *,
        review_round_id: int,
        reviewer_run_id: str,
        finding: Mapping[str, Any],
    ) -> int:
        response = self.tools.mcp_den_create_review_finding(
            review_round_id=review_round_id,
            created_by=f"{self.requested_by}-reviewer",
            category=str(finding.get("category", "acceptance_gap")),
            summary=str(finding.get("summary", "Reviewer finding")),
            notes=finding.get("notes"),
            file_references=_json_or_none(finding.get("file_references")),
            test_commands=_json_or_none(finding.get("test_commands")),
            run_id=reviewer_run_id,
            subagent_role="reviewer",
        )
        payload = _coerce_mapping_response(response)
        return int(payload.get("id"))

    def post_review_findings_and_verdict(
        self,
        *,
        task_id: int,
        review_request: Mapping[str, Any],
        reviewer_run_id: str,
        verdict: str,
        summary: str,
    ) -> None:
        review_round_id = _review_round_id(review_request)
        thread_id = _review_thread_id(review_request)
        reviewer = f"{self.requested_by}-reviewer"
        findings_response = self.tools.mcp_den_post_review_findings(
            project_id=self.project_id,
            task_id=task_id,
            review_round_id=review_round_id,
            sender=reviewer,
            thread_id=thread_id,
            notes=summary,
            run_id=reviewer_run_id,
            subagent_role="reviewer",
        )
        _ensure_den_did_not_reject(
            _coerce_mapping_response(findings_response),
            context=f"review findings publication for {reviewer_run_id}",
        )
        verdict_response = self.tools.mcp_den_set_review_verdict(
            review_round_id=review_round_id,
            verdict=verdict,
            decided_by=reviewer,
            notes=summary,
            run_id=reviewer_run_id,
            subagent_role="reviewer",
        )
        _ensure_den_did_not_reject(
            _coerce_mapping_response(verdict_response),
            context=f"review verdict publication for {reviewer_run_id}",
        )

    def respond_to_review_finding(
        self,
        *,
        finding_id: int,
        response_notes: str,
        status: str,
    ) -> Mapping[str, Any]:
        response = self.tools.mcp_den_respond_to_review_finding(
            review_finding_id=finding_id,
            responded_by=self.requested_by,
            response_notes=response_notes,
            status=status,
            status_notes=response_notes,
        )
        payload = _coerce_mapping_response(response)
        _ensure_den_did_not_reject(payload, context=f"review finding response for {finding_id}")
        return payload


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
        claimed_finding_ids=[int(value) for value in worker.artifact.get("claimed_finding_ids", [])],
        response_notes=worker.artifact.get("response_notes"),
    )


def run_tracked_reviewer_path(
    adapter: DenWorkflowAdapter,
    *,
    task_id: int,
    prompt: str,
    run_id: str,
    coder_artifact: Mapping[str, Any],
    cwd: str | Path | None = None,
    env_overrides: Mapping[str, str] | None = None,
    runtime_registry_path: str | Path | None = None,
    review_request: Mapping[str, Any] | None = None,
    base_branch: str | None = None,
    base_commit: str | None = None,
) -> ReviewerPathResult:
    """Run the tracked spawned-Hermes reviewer path after coder completion."""

    missing = [field for field in ("branch", "head_commit", "tests_run") if not coder_artifact.get(field)]
    if missing:
        return ReviewerPathResult(
            status="failed",
            run_id=run_id,
            error=f"Coder artifact missing required reviewer input fields: {', '.join(missing)}",
        )
    branch = str(coder_artifact["branch"])
    head_commit = str(coder_artifact["head_commit"])
    tests_run = list(coder_artifact.get("tests_run") or [])
    try:
        if review_request is None:
            review_request = adapter.request_review(
                task_id=task_id,
                branch=branch,
                head_commit=head_commit,
                tests_run=tests_run,
                coder_run_id=coder_artifact.get("run_id"),
                base_branch=base_branch,
                base_commit=base_commit,
            )
        review_round_id = _review_round_id(review_request)
        packet = adapter.prepare_reviewer_context_packet(
            task_id=task_id,
            review_round_id=review_round_id,
            branch=branch,
            head_commit=head_commit,
            base_branch=base_branch,
            base_commit=base_commit,
            notes="Prepared by spawned-Hermes orchestrator reviewer path.",
        )
        packet_message_id = _packet_message_id(packet)
        runtime = resolve_role_runtime(
            "reviewer",
            registry_path=_selected_runtime_registry_path(runtime_registry_path),
            run_id=run_id,
        )
    except (RuntimeRegistryError, KeyError, TypeError, ValueError) as exc:
        return ReviewerPathResult(status="failed", run_id=run_id, review_request=review_request, error=str(exc))

    artifact_path = runtime.artifact_path or str(Path(runtime.run_root) / run_id / runtime.artifact_filename)
    log_path = runtime.log_path or str(Path(runtime.run_root) / run_id / runtime.log_filename)
    try:
        adapter.register_worker_run(
            task_id=task_id,
            run_id=run_id,
            role="reviewer",
            branch=branch,
            head_commit=head_commit,
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
            dedupe_key=f"{task_id}:reviewer:{run_id}",
        )
    except Exception as exc:  # noqa: BLE001 - fail closed before subprocess launch
        return ReviewerPathResult(status="failed", run_id=run_id, review_request=review_request, artifact_path=artifact_path, error=str(exc))

    adapter.mark_worker_started(task_id=task_id, run_id=run_id, role="reviewer")
    worker = run_hermes_worker(
        task_id=task_id,
        run_id=run_id,
        role="reviewer",
        prompt=_reviewer_prompt_with_packet(
            prompt=prompt,
            packet_message_id=packet_message_id,
            branch=branch,
            head_commit=head_commit,
            tests_run=tests_run,
        ),
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
        error = worker.error or "Reviewer worker did not complete"
        adapter.mark_worker_failed(task_id=task_id, run_id=run_id, role="reviewer", error=error)
        return ReviewerPathResult(status="failed", run_id=run_id, review_request=review_request, artifact_path=artifact_path, error=error)

    try:
        finding_ids = [
            adapter.create_review_finding(
                review_round_id=review_round_id,
                reviewer_run_id=run_id,
                finding=finding,
            )
            for finding in worker.artifact.get("findings", [])
        ]
        reviewer_artifact = {**worker.artifact, "finding_ids": finding_ids}
        adapter.mark_worker_completed(task_id=task_id, run_id=run_id, role="reviewer", artifact=reviewer_artifact)
        verdict = str(worker.artifact["verdict"])
        adapter.post_review_findings_and_verdict(
            task_id=task_id,
            review_request=review_request,
            reviewer_run_id=run_id,
            verdict=verdict,
            summary=str(worker.artifact.get("summary", "")),
        )
    except Exception as exc:  # noqa: BLE001 - publication failures are fail-closed
        return ReviewerPathResult(status="failed", run_id=run_id, review_request=review_request, artifact_path=artifact_path, error=str(exc))

    latest_completion = adapter.get_latest_worker_completion(task_id=task_id, run_id=run_id, role="reviewer")
    worker_status = adapter.get_worker_run_status(task_id=task_id, run_id=run_id)
    return ReviewerPathResult(
        status="completed",
        run_id=run_id,
        verdict=verdict,
        finding_ids=finding_ids,
        artifact_path=artifact_path,
        latest_completion=latest_completion,
        worker_status=worker_status,
        review_request=review_request,
    )


def handle_review_outcome(
    adapter: DenWorkflowAdapter,
    *,
    task_id: int,
    review_state: Mapping[str, Any],
    prompt: str,
    next_coder_run_id: str,
    max_attempts: int = 3,
    coder_runner: Any = run_tracked_coder_path,
    **coder_kwargs: Any,
) -> ReviewLoopResult:
    """Handle review verdicts and bounded coder retry policy."""

    verdict = str(review_state.get("verdict", ""))
    findings = [finding for finding in review_state.get("findings", []) if isinstance(finding, Mapping)]
    finding_ids = [int(finding["id"]) for finding in findings if finding.get("id") is not None]
    blocking_findings = [finding for finding in findings if str(finding.get("category")) != "follow_up_candidate"]
    if verdict == "looks_good":
        return ReviewLoopResult(status="done_ready", reason="review verdict looks_good", finding_ids=finding_ids)
    if findings and not blocking_findings:
        return ReviewLoopResult(status="follow_up_deferred", reason="only follow-up candidate findings remain", finding_ids=finding_ids)
    if verdict != "changes_requested":
        return ReviewLoopResult(status="blocked", reason=f"unhandled review verdict: {verdict}", finding_ids=finding_ids)

    attempt = int(review_state.get("attempt", 1))
    if attempt >= max_attempts:
        return ReviewLoopResult(status="blocked", reason=f"max attempts reached ({attempt}/{max_attempts})", finding_ids=finding_ids)
    stale_reason = _stale_review_reason(adapter=adapter, task_id=task_id, review_state=review_state)
    if stale_reason is not None:
        return ReviewLoopResult(status="blocked", reason=stale_reason, finding_ids=finding_ids)
    retry_prompt = _retry_prompt_with_findings(prompt=prompt, findings=blocking_findings, attempt=attempt + 1, max_attempts=max_attempts)
    coder_result = coder_runner(
        adapter,
        task_id=task_id,
        prompt=retry_prompt,
        run_id=next_coder_run_id,
        **coder_kwargs,
    )
    if coder_result.status != "completed":
        return ReviewLoopResult(
            status="blocked",
            reason=coder_result.error or "coder retry failed",
            run_id=next_coder_run_id,
            finding_ids=finding_ids,
            coder_result=coder_result,
        )
    for finding in blocking_findings:
        finding_id = finding.get("id")
        if finding_id is not None and int(finding_id) in set(coder_result.claimed_finding_ids):
            adapter.respond_to_review_finding(
                finding_id=int(finding_id),
                response_notes=coder_result.response_notes or f"Coder retry {next_coder_run_id} claims this finding is fixed.",
                status="claimed_fixed",
            )
    return ReviewLoopResult(
        status="retry_launched",
        reason="changes_requested findings sent to coder retry",
        run_id=next_coder_run_id,
        finding_ids=finding_ids,
        coder_result=coder_result,
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


def _packet_type_for_role(role: str) -> str:
    return {
        "coder": "implementation_packet",
        "reviewer": "review_findings_packet",
        "validator": "validation_packet",
        "drift_checker": "drift_check_packet",
        "packet_auditor": "packet_audit_packet",
    }.get(role, "worker_failure_packet")


def _json_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value)


def _review_round_id(review_request: Mapping[str, Any]) -> int:
    value = review_request.get("review_round_id", review_request.get("id"))
    if value is None:
        raise ValueError("review_request must include review_round_id or id")
    return int(value)


def _review_thread_id(review_request: Mapping[str, Any]) -> int | None:
    value = review_request.get("message_id", review_request.get("thread_id"))
    return int(value) if value is not None else None


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


def _reviewer_prompt_with_packet(
    *,
    prompt: str,
    packet_message_id: int,
    branch: str,
    head_commit: str,
    tests_run: list[Any],
) -> str:
    return (
        f"{prompt.rstrip()}\n\n"
        "DEN REVIEWER CONTEXT PACKET\n"
        f"Use Den task-thread packet message id {packet_message_id} as the bounded reviewer context source.\n"
        "CODER COMPLETION TO REVIEW\n"
        f"Branch: {branch}\n"
        f"Head commit: {head_commit}\n"
        f"Tests run: {json.dumps(tests_run, sort_keys=True)}\n"
    )


def _retry_prompt_with_findings(
    *,
    prompt: str,
    findings: list[Mapping[str, Any]],
    attempt: int,
    max_attempts: int,
) -> str:
    finding_lines = [f"- #{finding.get('id')}: {finding.get('summary')}" for finding in findings]
    return (
        f"{prompt.rstrip()}\n\n"
        f"REVIEW RETRY ATTEMPT {attempt}/{max_attempts}\n"
        "Address these blocking review findings before reporting completion:\n"
        + "\n".join(finding_lines)
        + "\n"
    )


def _stale_review_reason(*, adapter: DenWorkflowAdapter, task_id: int, review_state: Mapping[str, Any]) -> str | None:
    summary = adapter.get_task_workflow_summary(task_id=task_id)
    current_review = summary.get("current_review_state")
    if not isinstance(current_review, Mapping):
        return None
    expected_round = review_state.get("review_round_id")
    current_round = current_review.get("review_round_id", current_review.get("id"))
    current_verdict = current_review.get("verdict")
    if expected_round is not None and current_round is not None and int(current_round) != int(expected_round):
        return f"newer review state exists: round {current_round} supersedes {expected_round}"
    if current_verdict in {"looks_good", "follow_up_needed"}:
        return f"newer review state is already terminal: {current_verdict}"
    return None


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

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class DenMcpAdapter:
    """Adapter from spawned-Hermes workflow events to Den MCP tool calls.

    The `tools` object is intentionally injected so tests can use a recorder and
    the runner can pass an object exposing the real `mcp_den_*` callables.
    """

    tools: Any
    project_id: str
    requested_by: str
    base_branch: str
    base_commit: str
    reviewer_identity: str | None = None

    def mark_worker_started(self, *, task_id: int, run_id: str, role: str) -> Any:
        return self.tools.mcp_den_send_message(
            project_id=self.project_id,
            sender=self.requested_by,
            task_id=task_id,
            content=f"Spawned-Hermes {role} worker `{run_id}` started.",
            metadata={"type": "spawned_hermes_worker_started", "run_id": run_id, "role": role},
            intent="handoff",
        )

    def register_worker_run(
        self,
        *,
        task_id: int,
        run_id: str,
        role: str,
        session_id: str | None = None,
        branch: str | None = None,
        base_branch: str | None = None,
        base_commit: str | None = None,
        head_commit: str | None = None,
        profile: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        toolsets: Sequence[str] | str | None = None,
        workdir: str | None = None,
        host: str | None = None,
        timeout_seconds: int | None = None,
        artifact_path: str | None = None,
        log_path: str | None = None,
        prompt_packet_message_id: int | None = None,
        state_file_ref: str | None = None,
        dedupe_key: str | None = None,
    ) -> Any:
        args: dict[str, Any] = {
            "project_id": self.project_id,
            "task_id": task_id,
            "requested_by": self.requested_by,
            "role": role,
            "substrate": "spawned_hermes",
            "run_id": run_id,
        }
        optional = {
            "session_id": session_id,
            "branch": branch,
            "base_branch": base_branch or self.base_branch,
            "base_commit": base_commit or self.base_commit,
            "head_commit": head_commit,
            "profile": profile,
            "provider": provider,
            "model": model,
            "toolsets": _csv_or_none(toolsets),
            "workdir": workdir,
            "host": host,
            "timeout_seconds": timeout_seconds,
            "artifact_path": artifact_path,
            "log_path": log_path,
            "prompt_packet_message_id": prompt_packet_message_id,
            "state_file_ref": state_file_ref,
            "dedupe_key": dedupe_key,
        }
        args.update({key: value for key, value in optional.items() if value is not None})
        register_tool = getattr(self.tools, "mcp_den_register_worker_run", None)
        if register_tool is None:
            raise RuntimeError(
                "Den MCP tool mcp_den_register_worker_run is unavailable; deploy the spawned-Hermes "
                "worker registration API before launching tracked local workers."
            )
        response = register_tool(**args)
        _ensure_worker_registration_accepted(response, run_id=run_id, role=role)
        return response

    def mark_worker_completed(self, *, task_id: int, run_id: str, role: str, artifact: Mapping[str, Any]) -> Any:
        response = self.tools.mcp_den_post_worker_completion_packet(
            **self._completion_packet_args(task_id=task_id, run_id=run_id, role=role, artifact=artifact)
        )
        _ensure_completion_packet_accepted(response, run_id=run_id, role=role)
        return response

    def mark_worker_failed(self, *, task_id: int, run_id: str, role: str, error: str) -> Any:
        response = self.tools.mcp_den_post_worker_completion_packet(
            project_id=self.project_id,
            run_id=run_id,
            requested_by=self.requested_by,
            status="failed",
            role=role,
            packet_type="worker_failure_packet",
            summary=error,
            failure_category="spawned_hermes_worker_failed",
            recovery_guidance=(
                "Inspect spawned-Hermes stdout/stderr and completion artifact path, "
                "then rerun or abort the local worker."
            ),
            dedupe_key=f"{run_id}:failed",
        )
        _ensure_completion_packet_accepted(response, run_id=run_id, role=role)
        return response

    def get_latest_worker_completion(self, *, task_id: int, run_id: str, role: str | None = None) -> Any:
        args: dict[str, Any] = {
            "project_id": self.project_id,
            "task_id": task_id,
            "run_id": run_id,
        }
        if role is not None:
            args["role"] = role
        return self.tools.mcp_den_get_latest_worker_completion(**args)

    def get_worker_run_status(self, *, task_id: int, run_id: str) -> Any:
        return self.tools.mcp_den_get_worker_run_status(
            project_id=self.project_id,
            task_id=task_id,
            run_id=run_id,
        )

    def request_review(
        self,
        *,
        task_id: int,
        branch: str,
        head_commit: str,
        tests_run: Sequence[Any],
        coder_run_id: str,
    ) -> Any:
        return self.tools.mcp_den_request_review(
            project_id=self.project_id,
            task_id=task_id,
            requested_by=self.requested_by,
            branch=branch,
            base_branch=self.base_branch,
            base_commit=self.base_commit,
            head_commit=head_commit,
            tests_run=json.dumps(list(tests_run)),
            notes=f"Spawned-Hermes coder run {coder_run_id} produced verified branch/head evidence.",
            run_id=coder_run_id,
        )

    def post_review_findings(
        self,
        *,
        task_id: int,
        review_request: Mapping[str, Any],
        reviewer_run_id: str,
        verdict: str,
        findings: Sequence[Mapping[str, Any]],
        summary: str,
    ) -> None:
        review_round_id = _review_round_id(review_request)
        thread_id = _review_thread_id(review_request)
        reviewer = self.reviewer_identity or f"{self.requested_by}-reviewer"

        for finding in findings:
            self.tools.mcp_den_create_review_finding(
                review_round_id=review_round_id,
                created_by=reviewer,
                category=str(finding.get("category", "acceptance_gap")),
                summary=str(finding.get("summary", "Reviewer finding")),
                notes=finding.get("notes"),
                file_references=_json_or_none(finding.get("file_references")),
                test_commands=_json_or_none(finding.get("test_commands")),
                run_id=reviewer_run_id,
                subagent_role="reviewer",
            )

        self.tools.mcp_den_post_review_findings(
            project_id=self.project_id,
            task_id=task_id,
            review_round_id=review_round_id,
            sender=reviewer,
            thread_id=thread_id,
            notes=summary,
            run_id=reviewer_run_id,
            subagent_role="reviewer",
        )
        self.tools.mcp_den_set_review_verdict(
            review_round_id=review_round_id,
            verdict=verdict,
            decided_by=reviewer,
            notes=summary,
            run_id=reviewer_run_id,
            subagent_role="reviewer",
        )

    def _completion_packet_args(
        self,
        *,
        task_id: int,
        run_id: str,
        role: str,
        artifact: Mapping[str, Any],
    ) -> dict[str, Any]:
        packet_type = _packet_type_for_role(role)
        args: dict[str, Any] = {
            "project_id": self.project_id,
            "run_id": run_id,
            "requested_by": self.requested_by,
            "status": str(artifact.get("status", "completed")),
            "role": role,
            "packet_type": packet_type,
            "summary": str(artifact.get("summary", "Spawned-Hermes worker completed.")),
            "dedupe_key": f"{run_id}:completed",
        }
        if role == "coder":
            args.update(
                {
                    "branch": artifact.get("branch"),
                    "head_commit": artifact.get("head_commit"),
                    "base_commit": artifact.get("base_commit", self.base_commit),
                    "tests_run": json.dumps(list(artifact.get("tests_run", []))),
                }
            )
        if role == "reviewer":
            args.update(
                {
                    "finding_ids": _json_or_none(artifact.get("finding_ids")),
                }
            )
        return args


def _packet_type_for_role(role: str) -> str:
    return {
        "coder": "implementation_packet",
        "reviewer": "review_findings_packet",
        "validator": "validation_packet",
        "drift_checker": "drift_check_packet",
        "packet_auditor": "packet_audit_packet",
    }.get(role, "worker_failure_packet")


def _ensure_worker_registration_accepted(response: Any, *, run_id: str, role: str) -> None:
    payload = _response_payload(response)
    if not isinstance(payload, Mapping):
        return
    worker_run = payload.get("worker_run")
    if isinstance(worker_run, Mapping):
        returned_run_id = worker_run.get("run_id")
        if returned_run_id is not None and returned_run_id != run_id:
            raise RuntimeError(
                f"Den registered {role} worker under unexpected run_id {returned_run_id!r}; expected {run_id!r}"
            )
    if payload.get("error") or payload.get("status") == "error" or payload.get("failure_category"):
        summary = payload.get("summary") or payload.get("error") or "Den rejected worker registration"
        raise RuntimeError(f"Den rejected {role} worker registration for run {run_id}: {summary}")


def _ensure_completion_packet_accepted(response: Any, *, run_id: str, role: str) -> None:
    payload = _response_payload(response)
    if not isinstance(payload, Mapping):
        return
    completion_state = payload.get("completion_state")
    failure_category = payload.get("failure_category")
    if completion_state in {"missing_run", "malformed", "rejected"} or failure_category:
        summary = payload.get("summary") or payload.get("error") or "Den rejected worker completion packet"
        raise RuntimeError(
            f"Den rejected {role} completion packet for run {run_id}: "
            f"completion_state={completion_state!r}, failure_category={failure_category!r}, summary={summary}"
        )


def _response_payload(response: Any) -> Any:
    if isinstance(response, str):
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            return response
    if isinstance(response, Mapping) and isinstance(response.get("result"), str):
        try:
            return json.loads(response["result"])
        except json.JSONDecodeError:
            return response
    return response


def _csv_or_none(value: Sequence[str] | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return ",".join(str(item) for item in value)


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

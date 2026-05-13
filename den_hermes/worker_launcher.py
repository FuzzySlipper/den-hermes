from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from den_hermes.runtime_registry import RuntimeRegistryError, resolve_role_runtime


@dataclass(frozen=True)
class HermesWorkerResult:
    status: str
    exit_code: int | None
    stdout: str
    stderr: str
    artifact: dict[str, Any] | None = None
    error: str | None = None
    command: tuple[str, ...] = ()


@dataclass(frozen=True)
class CoderReviewerSequenceResult:
    status: str
    coder: HermesWorkerResult
    reviewer: HermesWorkerResult | None = None
    error: str | None = None


@dataclass(frozen=True)
class DenCoderReviewerWorkflowResult:
    status: str
    coder: HermesWorkerResult
    reviewer: HermesWorkerResult | None = None
    review_request: Any | None = None
    error: str | None = None


def run_hermes_worker(
    *,
    task_id: int,
    run_id: str,
    role: str,
    prompt: str,
    expected_artifact: str | Path,
    provider: str | None = None,
    model: str | None = None,
    profile: str | None = None,
    toolsets: Sequence[str] | None = None,
    cwd: str | Path | None = None,
    env_overrides: Mapping[str, str] | None = None,
    timeout_seconds: int = 300,
) -> HermesWorkerResult:
    """Run a Hermes worker process and verify its completion artifact.

    This is intentionally small: it proves the subprocess + artifact contract for
    a spawned-Hermes worker without depending on real LLM calls in tests.
    """

    artifact_path = Path(expected_artifact)
    command = _build_command(
        prompt=_inject_artifact_contract(prompt, artifact_path),
        provider=provider,
        model=model,
        profile=profile,
        toolsets=toolsets,
    )
    env = os.environ.copy()
    env.update(
        {
            "DEN_TASK_ID": str(task_id),
            "DEN_RUN_ID": run_id,
            "DEN_WORKER_ROLE": role,
            "DEN_EXPECTED_ARTIFACT": str(artifact_path),
        }
    )
    if env_overrides:
        env.update(env_overrides)

    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return HermesWorkerResult(
            status="failed",
            exit_code=None,
            stdout=exc.stdout or "",
            stderr=exc.stderr or "",
            error=f"Hermes worker timed out after {timeout_seconds} seconds",
            command=tuple(command),
        )

    if completed.returncode != 0:
        return HermesWorkerResult(
            status="failed",
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            error=f"Hermes worker exited with code {completed.returncode}",
            command=tuple(command),
        )

    if not artifact_path.exists():
        return HermesWorkerResult(
            status="incomplete",
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            error=f"Missing completion artifact: {artifact_path}",
            command=tuple(command),
        )

    try:
        artifact = json.loads(artifact_path.read_text())
    except Exception as exc:  # noqa: BLE001 - preserve concise fail-closed result
        return HermesWorkerResult(
            status="failed",
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            error=f"Invalid completion artifact JSON at {artifact_path}: {exc}",
            command=tuple(command),
        )

    validation_error = _validate_artifact_identity(
        artifact=artifact,
        task_id=task_id,
        run_id=run_id,
        role=role,
    )
    if validation_error is None:
        validation_error = _validate_artifact_shape(artifact=artifact, role=role)
    if validation_error:
        return HermesWorkerResult(
            status="failed",
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            error=validation_error,
            command=tuple(command),
        )

    return HermesWorkerResult(
        status=str(artifact.get("status", "completed")),
        exit_code=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        artifact=artifact,
        command=tuple(command),
    )


def run_coder_reviewer_sequence(
    *,
    task_id: int,
    prompt: str,
    run_root: str | Path,
    coder: Mapping[str, Any],
    reviewer: Mapping[str, Any],
    cwd: str | Path | None = None,
    env_overrides: Mapping[str, str] | None = None,
    timeout_seconds: int = 300,
    verify_git: bool = False,
) -> CoderReviewerSequenceResult:
    """Run a fakeable coder -> reviewer sequence with artifact handoff."""

    run_root_path = Path(run_root)
    coder_run_id = str(coder["run_id"])
    coder_result = run_hermes_worker(
        task_id=task_id,
        run_id=coder_run_id,
        role="coder",
        prompt=prompt,
        expected_artifact=run_root_path / coder_run_id / "completion.json",
        provider=_optional_str(coder, "provider"),
        model=_optional_str(coder, "model"),
        profile=_optional_str(coder, "profile"),
        toolsets=coder.get("toolsets"),
        cwd=cwd,
        env_overrides=env_overrides,
        timeout_seconds=timeout_seconds,
    )
    if coder_result.status != "completed" or coder_result.artifact is None:
        return CoderReviewerSequenceResult(
            status="failed",
            coder=coder_result,
            error=coder_result.error or "Coder worker did not complete",
        )
    if verify_git:
        git_error = _verify_git_branch_head(coder_result.artifact, cwd=cwd)
        if git_error:
            return CoderReviewerSequenceResult(
                status="failed",
                coder=coder_result,
                error=git_error,
            )

    reviewer_run_id = str(reviewer["run_id"])
    reviewer_prompt = (
        f"{prompt.rstrip()}\n\n"
        "CODER COMPLETION TO REVIEW\n"
        f"Branch: {coder_result.artifact['branch']}\n"
        f"Head commit: {coder_result.artifact['head_commit']}\n"
        f"Tests run: {json.dumps(coder_result.artifact['tests_run'], sort_keys=True)}\n"
        f"Coder summary: {coder_result.artifact.get('summary', '')}\n"
    )
    reviewer_result = run_hermes_worker(
        task_id=task_id,
        run_id=reviewer_run_id,
        role="reviewer",
        prompt=reviewer_prompt,
        expected_artifact=run_root_path / reviewer_run_id / "completion.json",
        provider=_optional_str(reviewer, "provider"),
        model=_optional_str(reviewer, "model"),
        profile=_optional_str(reviewer, "profile"),
        toolsets=reviewer.get("toolsets"),
        cwd=cwd,
        env_overrides=env_overrides,
        timeout_seconds=timeout_seconds,
    )
    if reviewer_result.status != "completed" or reviewer_result.artifact is None:
        return CoderReviewerSequenceResult(
            status="failed",
            coder=coder_result,
            reviewer=reviewer_result,
            error=reviewer_result.error or "Reviewer worker did not complete",
        )

    return CoderReviewerSequenceResult(
        status="completed",
        coder=coder_result,
        reviewer=reviewer_result,
    )


def run_den_coder_reviewer_workflow(
    *,
    den_client: Any,
    task_id: int,
    prompt: str,
    run_root: str | Path,
    coder: Mapping[str, Any],
    reviewer: Mapping[str, Any],
    cwd: str | Path | None = None,
    env_overrides: Mapping[str, str] | None = None,
    timeout_seconds: int = 300,
    runtime_registry_path: str | Path | None = None,
    verify_git: bool = False,
) -> DenCoderReviewerWorkflowResult:
    """Run coder -> Den review request -> reviewer with a fakeable Den client.

    This bridge-shaped wrapper records worker lifecycle transitions through a
    deliberately small adapter interface, and it only requests review after a
    coder artifact has passed local validation and optional git verification.
    """

    run_root_path = Path(run_root)
    cwd_path = str(cwd) if cwd is not None else None
    try:
        coder_runtime = _resolve_workflow_worker_config(
            role="coder",
            worker=coder,
            registry_path=runtime_registry_path,
            run_id=str(coder["run_id"]),
        )
        reviewer_runtime = _resolve_workflow_worker_config(
            role="reviewer",
            worker=reviewer,
            registry_path=runtime_registry_path,
            run_id=str(reviewer["run_id"]),
        )
    except (KeyError, RuntimeRegistryError, ValueError) as exc:
        error = f"Runtime resolver failed: {exc}"
        return DenCoderReviewerWorkflowResult(
            status="failed",
            coder=HermesWorkerResult(status="failed", exit_code=None, stdout="", stderr="", error=error),
            error=error,
        )
    coder_timeout_seconds = int(coder_runtime.get("timeout_seconds", timeout_seconds))
    reviewer_timeout_seconds = int(reviewer_runtime.get("timeout_seconds", timeout_seconds))
    coder_run_id = str(coder_runtime["run_id"])
    coder_artifact_path = run_root_path / coder_run_id / "completion.json"
    try:
        den_client.register_worker_run(
            task_id=task_id,
            run_id=coder_run_id,
            role="coder",
            profile=_optional_str(coder_runtime, "profile"),
            provider=_optional_str(coder_runtime, "provider"),
            model=_optional_str(coder_runtime, "model"),
            toolsets=coder_runtime.get("toolsets"),
            workdir=cwd_path,
            timeout_seconds=coder_timeout_seconds,
            artifact_path=str(coder_artifact_path),
            log_path=str(run_root_path / coder_run_id / "worker.log"),
            runtime_id=_optional_str(coder_runtime, "runtime_id"),
            dedupe_key=f"{task_id}:coder:{coder_run_id}",
        )
    except Exception as exc:  # noqa: BLE001 - fail closed before subprocess launch
        error = f"Coder worker registration failed: {exc}"
        return DenCoderReviewerWorkflowResult(
            status="failed",
            coder=HermesWorkerResult(status="failed", exit_code=None, stdout="", stderr="", error=error),
            error=error,
        )
    den_client.mark_worker_started(task_id=task_id, run_id=coder_run_id, role="coder")
    coder_result = run_hermes_worker(
        task_id=task_id,
        run_id=coder_run_id,
        role="coder",
        prompt=prompt,
        expected_artifact=coder_artifact_path,
        provider=_optional_str(coder_runtime, "provider"),
        model=_optional_str(coder_runtime, "model"),
        profile=_optional_str(coder_runtime, "profile"),
        toolsets=coder_runtime.get("toolsets"),
        cwd=cwd,
        env_overrides=env_overrides,
        timeout_seconds=coder_timeout_seconds,
    )
    if coder_result.status == "completed" and coder_result.artifact is not None:
        try:
            den_client.mark_worker_completed(
                task_id=task_id,
                run_id=coder_run_id,
                role="coder",
                artifact=coder_result.artifact,
            )
        except Exception as exc:  # noqa: BLE001 - Den rejected authoritative completion; fail closed
            error = f"Coder completion rejected by Den: {exc}"
            return DenCoderReviewerWorkflowResult(status="failed", coder=coder_result, error=error)
    else:
        error = coder_result.error or "Coder worker did not complete"
        den_client.mark_worker_failed(task_id=task_id, run_id=coder_run_id, role="coder", error=error)
        return DenCoderReviewerWorkflowResult(status="failed", coder=coder_result, error=error)

    if verify_git:
        git_error = _verify_git_branch_head(coder_result.artifact, cwd=cwd)
        if git_error:
            den_client.mark_worker_failed(task_id=task_id, run_id=coder_run_id, role="coder", error=git_error)
            return DenCoderReviewerWorkflowResult(status="failed", coder=coder_result, error=git_error)

    review_request = den_client.request_review(
        task_id=task_id,
        branch=coder_result.artifact["branch"],
        head_commit=coder_result.artifact["head_commit"],
        tests_run=coder_result.artifact["tests_run"],
        coder_run_id=coder_run_id,
    )

    reviewer_run_id = str(reviewer_runtime["run_id"])
    reviewer_artifact_path = run_root_path / reviewer_run_id / "completion.json"
    try:
        den_client.register_worker_run(
            task_id=task_id,
            run_id=reviewer_run_id,
            role="reviewer",
            branch=coder_result.artifact["branch"],
            head_commit=coder_result.artifact["head_commit"],
            profile=_optional_str(reviewer_runtime, "profile"),
            provider=_optional_str(reviewer_runtime, "provider"),
            model=_optional_str(reviewer_runtime, "model"),
            toolsets=reviewer_runtime.get("toolsets"),
            workdir=cwd_path,
            timeout_seconds=reviewer_timeout_seconds,
            artifact_path=str(reviewer_artifact_path),
            log_path=str(run_root_path / reviewer_run_id / "worker.log"),
            runtime_id=_optional_str(reviewer_runtime, "runtime_id"),
            dedupe_key=f"{task_id}:reviewer:{reviewer_run_id}",
        )
    except Exception as exc:  # noqa: BLE001 - fail closed before reviewer subprocess launch
        error = f"Reviewer worker registration failed: {exc}"
        return DenCoderReviewerWorkflowResult(
            status="failed",
            coder=coder_result,
            reviewer=HermesWorkerResult(status="failed", exit_code=None, stdout="", stderr="", error=error),
            review_request=review_request,
            error=error,
        )
    den_client.mark_worker_started(task_id=task_id, run_id=reviewer_run_id, role="reviewer")
    reviewer_prompt = (
        f"{prompt.rstrip()}\n\n"
        "CODER COMPLETION TO REVIEW\n"
        f"Branch: {coder_result.artifact['branch']}\n"
        f"Head commit: {coder_result.artifact['head_commit']}\n"
        f"Tests run: {json.dumps(coder_result.artifact['tests_run'], sort_keys=True)}\n"
        f"Coder summary: {coder_result.artifact.get('summary', '')}\n"
    )
    reviewer_result = run_hermes_worker(
        task_id=task_id,
        run_id=reviewer_run_id,
        role="reviewer",
        prompt=reviewer_prompt,
        expected_artifact=reviewer_artifact_path,
        provider=_optional_str(reviewer_runtime, "provider"),
        model=_optional_str(reviewer_runtime, "model"),
        profile=_optional_str(reviewer_runtime, "profile"),
        toolsets=reviewer_runtime.get("toolsets"),
        cwd=cwd,
        env_overrides=env_overrides,
        timeout_seconds=reviewer_timeout_seconds,
    )
    if reviewer_result.status != "completed" or reviewer_result.artifact is None:
        error = reviewer_result.error or "Reviewer worker did not complete"
        den_client.mark_worker_failed(task_id=task_id, run_id=reviewer_run_id, role="reviewer", error=error)
        return DenCoderReviewerWorkflowResult(
            status="failed",
            coder=coder_result,
            reviewer=reviewer_result,
            review_request=review_request,
            error=error,
        )

    try:
        den_client.mark_worker_completed(
            task_id=task_id,
            run_id=reviewer_run_id,
            role="reviewer",
            artifact=reviewer_result.artifact,
        )
    except Exception as exc:  # noqa: BLE001 - Den rejected authoritative completion; fail closed
        error = f"Reviewer completion rejected by Den: {exc}"
        return DenCoderReviewerWorkflowResult(
            status="failed",
            coder=coder_result,
            reviewer=reviewer_result,
            review_request=review_request,
            error=error,
        )
    den_client.post_review_findings(
        task_id=task_id,
        review_request=review_request,
        reviewer_run_id=reviewer_run_id,
        verdict=reviewer_result.artifact["verdict"],
        findings=reviewer_result.artifact["findings"],
        summary=reviewer_result.artifact.get("summary", ""),
    )
    return DenCoderReviewerWorkflowResult(
        status="completed",
        coder=coder_result,
        reviewer=reviewer_result,
        review_request=review_request,
    )


def _build_command(
    *,
    prompt: str,
    provider: str | None,
    model: str | None,
    profile: str | None,
    toolsets: Sequence[str] | None,
) -> list[str]:
    command = ["hermes"]
    if profile:
        command.extend(["--profile", profile])
    command.append("chat")
    if provider:
        command.extend(["--provider", provider])
    if model:
        command.extend(["--model", model])
    if toolsets:
        command.extend(["--toolsets", ",".join(toolsets)])
    command.extend(["--source", "den-worker", "-q", prompt])
    return command


def _inject_artifact_contract(prompt: str, expected_artifact: Path) -> str:
    return (
        f"{prompt.rstrip()}\n\n"
        "EXPECTED COMPLETION ARTIFACT\n"
        f"Write the Den worker completion JSON to: {expected_artifact}\n"
        "The parent orchestrator will fail closed if this file is missing, "
        "malformed, or has mismatched task/run/role identity.\n"
    )


def _validate_artifact_identity(
    *,
    artifact: Mapping[str, Any],
    task_id: int,
    run_id: str,
    role: str,
) -> str | None:
    if artifact.get("task_id") != task_id:
        return f"Task id mismatch in completion artifact: expected {task_id}, got {artifact.get('task_id')!r}"
    if artifact.get("run_id") != run_id:
        return f"Run id mismatch in completion artifact: expected {run_id}, got {artifact.get('run_id')!r}"
    if artifact.get("role") != role:
        return f"Role mismatch in completion artifact: expected {role}, got {artifact.get('role')!r}"
    return None


def _validate_artifact_shape(*, artifact: Mapping[str, Any], role: str) -> str | None:
    if role == "coder":
        for field in ("branch", "head_commit", "tests_run", "summary", "status"):
            if not artifact.get(field):
                return f"Missing required coder completion field: {field}"
        head_commit = artifact.get("head_commit")
        if not isinstance(head_commit, str) or len(head_commit) != 40:
            return "Invalid coder completion field: head_commit must be a full 40-character commit SHA"
        if not isinstance(artifact.get("tests_run"), list):
            return "Invalid coder completion field: tests_run must be a list"
    if role == "reviewer":
        for field in ("verdict", "findings", "summary", "status"):
            if field not in artifact:
                return f"Missing required reviewer completion field: {field}"
        if artifact.get("verdict") not in {"looks_good", "changes_requested", "blocked", "follow_up_needed"}:
            return f"Invalid reviewer verdict: {artifact.get('verdict')!r}"
        if not isinstance(artifact.get("findings"), list):
            return "Invalid reviewer completion field: findings must be a list"
    return None


def _resolve_workflow_worker_config(
    *,
    role: str,
    worker: Mapping[str, Any],
    registry_path: str | Path | None,
    run_id: str,
) -> dict[str, Any]:
    config = dict(worker)
    if registry_path is None:
        return config

    runtime_fields = {"profile", "provider", "model", "toolsets", "timeout_seconds", "runtime_id"}
    overrides = {key: config[key] for key in runtime_fields if key in config}
    runtime = resolve_role_runtime(
        role,
        registry_path=registry_path,
        run_id=run_id,
        overrides=overrides or None,
        allow_runtime_override=bool(config.get("allow_runtime_override", False)),
        override_reason=_optional_str(config, "override_reason"),
        requested_by=_optional_str(config, "requested_by"),
    )
    resolved = {
        "run_id": run_id,
        "runtime_id": runtime.runtime_id,
        "profile": runtime.profile,
        "provider": runtime.provider,
        "model": runtime.model,
        "toolsets": list(runtime.toolsets),
        "timeout_seconds": runtime.timeout_seconds,
    }
    if runtime.override is not None:
        resolved["runtime_override"] = dict(runtime.override)
    return resolved


def _optional_str(mapping: Mapping[str, Any], key: str) -> str | None:
    value = mapping.get(key)
    return str(value) if value is not None else None


def _verify_git_branch_head(artifact: Mapping[str, Any], cwd: str | Path | None) -> str | None:
    branch = str(artifact["branch"])
    head_commit = str(artifact["head_commit"])
    repo_cwd = str(cwd) if cwd is not None else None

    branch_ref = subprocess.run(
        ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
        cwd=repo_cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if branch_ref.returncode != 0:
        return f"Git verification failed: branch {branch!r} does not exist"

    head_ref = subprocess.run(
        ["git", "rev-parse", "--verify", f"{head_commit}^{{commit}}"],
        cwd=repo_cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if head_ref.returncode != 0:
        return f"Git verification failed: head_commit {head_commit!r} does not resolve"

    branch_sha = branch_ref.stdout.strip()
    head_sha = head_ref.stdout.strip()
    if branch_sha != head_sha:
        return (
            "Git verification failed: branch "
            f"{branch!r} points to {branch_sha}, not reported head_commit {head_sha}"
        )
    return None

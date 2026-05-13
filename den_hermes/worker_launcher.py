from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class HermesWorkerResult:
    status: str
    exit_code: int | None
    stdout: str
    stderr: str
    artifact: dict[str, Any] | None = None
    error: str | None = None
    command: tuple[str, ...] = ()


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

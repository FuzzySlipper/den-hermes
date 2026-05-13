from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass
class _TrackedProcess:
    task_id: int
    run_id: str
    role: str
    process: Any


class SpawnedHermesLifecycle:
    """Small bridge-side lifecycle helper for spawned-Hermes local processes.

    Den remains the durable source of truth. This helper only tracks best-effort
    local subprocess handles while the parent bridge process is alive, then
    reconciles terminal/abort outcomes back through the injected Den adapter.
    """

    def __init__(self, den_client: Any):
        self.den_client = den_client
        self._processes: dict[str, _TrackedProcess] = {}

    def track_process(self, *, task_id: int, run_id: str, role: str, process: Any) -> None:
        self._processes[run_id] = _TrackedProcess(task_id=task_id, run_id=run_id, role=role, process=process)

    def status(self, *, task_id: int, run_id: str) -> dict[str, Any]:
        den_status = self.den_client.get_worker_run_status(task_id=task_id, run_id=run_id)
        tracked = self._processes.get(run_id)
        return {
            "den": den_status,
            "local_process": self._local_process_status(tracked),
        }

    def abort(self, *, task_id: int, run_id: str, requested_by: str, reason: str | None = None) -> dict[str, Any]:
        tracked = self._processes.get(run_id)
        if tracked is None or tracked.process.poll() is not None:
            return {
                "status": "missing_local_process_handle",
                "run_id": run_id,
                "diagnostic": (
                    "Cannot abort spawned-Hermes run: no live local process handle is available in this "
                    "bridge process. The parent may have exited or the run may have been registered by another host."
                ),
            }

        tracked.process.terminate()
        try:
            tracked.process.wait(timeout=10)
        except TimeoutError:
            tracked.process.kill()
            tracked.process.wait(timeout=10)

        abort_reason = f"aborted by {requested_by}"
        if reason:
            abort_reason = f"{abort_reason}: {reason}"
        self.den_client.mark_worker_failed(
            task_id=task_id,
            run_id=run_id,
            role=tracked.role,
            error=abort_reason,
        )
        return {"status": "aborted", "run_id": run_id, "reason": reason}

    def cleanup_local_artifacts(
        self,
        *,
        artifact_path: str | Path | None = None,
        log_path: str | Path | None = None,
        terminal: bool,
    ) -> dict[str, Any]:
        if not terminal:
            return {
                "status": "blocked",
                "diagnostic": "cleanup requires a terminal Den worker state; active runs must finish or be aborted first",
            }

        removed: list[str] = []
        for candidate in (artifact_path, log_path):
            if candidate is None:
                continue
            path = Path(candidate)
            if path.exists():
                path.unlink()
                removed.append(str(path))

        return {
            "status": "cleaned_up" if removed else "noop",
            "removed": removed,
        }

    def rerun_config(self, worker_run: Mapping[str, Any], *, new_run_id: str) -> dict[str, Any]:
        metadata = dict(worker_run.get("launch_metadata") or {})
        metadata.pop("artifact_path", None)
        metadata.pop("log_path", None)
        metadata.pop("pid", None)
        metadata.pop("process_id", None)
        metadata["run_id"] = new_run_id
        metadata["rerun_of_run_id"] = worker_run.get("run_id")
        return metadata

    @staticmethod
    def _local_process_status(tracked: _TrackedProcess | None) -> dict[str, Any]:
        if tracked is None:
            return {"state": "missing_local_process_handle"}
        returncode = tracked.process.poll()
        if returncode is None:
            return {"state": "running", "returncode": None}
        return {"state": "exited", "returncode": returncode}

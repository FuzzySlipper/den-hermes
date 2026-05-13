from pathlib import Path

from den_hermes.worker_lifecycle import SpawnedHermesLifecycle


class FakeProcess:
    def __init__(self):
        self.terminated = False
        self.killed = False
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class FakeDenClient:
    def __init__(self):
        self.failed = []
        self.status_response = {"worker_run": {"run_id": "run-1", "state": "running", "status": "running"}}

    def mark_worker_failed(self, **kwargs):
        self.failed.append(kwargs)
        return {"completion_state": "present"}

    def get_worker_run_status(self, **kwargs):
        return self.status_response


def test_spawned_hermes_lifecycle_status_combines_den_and_local_process():
    den = FakeDenClient()
    lifecycle = SpawnedHermesLifecycle(den)
    process = FakeProcess()
    lifecycle.track_process(task_id=1378, run_id="run-1", role="coder", process=process)

    status = lifecycle.status(task_id=1378, run_id="run-1")

    assert status["den"]["worker_run"]["state"] == "running"
    assert status["local_process"]["state"] == "running"


def test_spawned_hermes_lifecycle_abort_terminates_process_and_reconciles_den():
    den = FakeDenClient()
    lifecycle = SpawnedHermesLifecycle(den)
    process = FakeProcess()
    lifecycle.track_process(task_id=1378, run_id="run-1", role="coder", process=process)

    result = lifecycle.abort(task_id=1378, run_id="run-1", requested_by="runner", reason="operator abort")

    assert result["status"] == "aborted"
    assert process.terminated is True
    assert den.failed[0]["run_id"] == "run-1"
    assert "operator abort" in den.failed[0]["error"]


def test_spawned_hermes_lifecycle_abort_without_process_handle_is_diagnostic_not_success():
    lifecycle = SpawnedHermesLifecycle(FakeDenClient())

    result = lifecycle.abort(task_id=1378, run_id="missing-run", requested_by="runner")

    assert result["status"] == "missing_local_process_handle"
    assert "no live local process handle" in result["diagnostic"]


def test_spawned_hermes_lifecycle_cleanup_is_idempotent_for_terminal_artifacts(tmp_path):
    artifact = tmp_path / "completion.json"
    log = tmp_path / "worker.log"
    artifact.write_text("{}")
    log.write_text("log")
    lifecycle = SpawnedHermesLifecycle(FakeDenClient())

    first = lifecycle.cleanup_local_artifacts(artifact_path=artifact, log_path=log, terminal=True)
    second = lifecycle.cleanup_local_artifacts(artifact_path=artifact, log_path=log, terminal=True)

    assert first["status"] == "cleaned_up"
    assert second["status"] == "noop"
    assert not artifact.exists()
    assert not log.exists()


def test_spawned_hermes_lifecycle_rerun_config_reuses_metadata_without_stale_artifacts():
    lifecycle = SpawnedHermesLifecycle(FakeDenClient())
    original = {
        "run_id": "old-run",
        "launch_metadata": {
            "profile": "den-hermes-worker",
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4",
            "toolsets": "terminal,file,mcp",
            "workdir": "/home/dev/den-hermes",
            "artifact_path": "/tmp/old/completion.json",
            "log_path": "/tmp/old/worker.log",
        },
    }

    config = lifecycle.rerun_config(original, new_run_id="new-run")

    assert config["run_id"] == "new-run"
    assert config["rerun_of_run_id"] == "old-run"
    assert config["profile"] == "den-hermes-worker"
    assert config["provider"] == "openrouter"
    assert "artifact_path" not in config
    assert "log_path" not in config

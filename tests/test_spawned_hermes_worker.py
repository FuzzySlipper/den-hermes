import json
import os
from pathlib import Path

from den_hermes.worker_launcher import run_hermes_worker


FAKE_HEAD = "0123456789abcdef0123456789abcdef01234567"


def install_fake_hermes(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    hermes = bin_dir / "hermes"
    hermes.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

log_path = Path(os.environ["FAKE_HERMES_LOG"])
artifact_path = Path(os.environ["DEN_EXPECTED_ARTIFACT"])

log_path.parent.mkdir(parents=True, exist_ok=True)
log_path.write_text(json.dumps({
    "argv": sys.argv,
    "cwd": os.getcwd(),
    "env": {
        "DEN_TASK_ID": os.environ.get("DEN_TASK_ID"),
        "DEN_RUN_ID": os.environ.get("DEN_RUN_ID"),
        "DEN_WORKER_ROLE": os.environ.get("DEN_WORKER_ROLE"),
        "DEN_EXPECTED_ARTIFACT": os.environ.get("DEN_EXPECTED_ARTIFACT"),
    },
}, indent=2))

mode = os.environ.get("FAKE_HERMES_MODE", "success")

if mode == "missing_artifact":
    print("fake hermes completed without writing artifact")
    raise SystemExit(0)

if mode == "bad_json":
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("{not json")
    raise SystemExit(0)

if mode == "nonzero":
    print("fake hermes failed", file=sys.stderr)
    raise SystemExit(42)

artifact_task_id = int(os.environ["DEN_TASK_ID"])
artifact_run_id = os.environ["DEN_RUN_ID"]
artifact_role = os.environ["DEN_WORKER_ROLE"]
if mode == "identity_mismatch":
    artifact_role = "coder" if artifact_role != "coder" else "reviewer"

artifact_path.parent.mkdir(parents=True, exist_ok=True)
artifact_path.write_text(json.dumps({
    "task_id": artifact_task_id,
    "run_id": artifact_run_id,
    "role": artifact_role,
    "status": "completed",
    "branch": "task/1368-fake",
    "head_commit": "0123456789abcdef0123456789abcdef01234567",
    "tests_run": [
        {"command": "pytest tests/ -q", "result": "passed"}
    ],
    "summary": "fake worker completed",
}, indent=2))
print("fake hermes ok")
raise SystemExit(0)
"""
    )
    hermes.chmod(0o755)
    return bin_dir


def fake_env(tmp_path: Path, mode: str = "success") -> dict[str, str]:
    bin_dir = install_fake_hermes(tmp_path)
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_HERMES_LOG": str(tmp_path / "fake-hermes-call.json"),
    }
    if mode != "success":
        env["FAKE_HERMES_MODE"] = mode
    return env


def test_spawned_hermes_worker_success_captures_command_and_artifact(tmp_path):
    artifact_path = tmp_path / ".den" / "runs" / "run-123" / "completion.json"

    result = run_hermes_worker(
        task_id=1368,
        run_id="run-123",
        role="coder",
        prompt="Implement task 1368 using the provided Den context.",
        expected_artifact=artifact_path,
        provider="openrouter",
        model="anthropic/claude-sonnet-4",
        profile="den-coder",
        toolsets=["terminal", "file", "mcp"],
        cwd=tmp_path,
        env_overrides=fake_env(tmp_path),
    )

    assert result.status == "completed"
    assert result.exit_code == 0
    assert result.artifact["branch"] == "task/1368-fake"
    assert result.artifact["head_commit"] == FAKE_HEAD
    assert result.stdout.strip() == "fake hermes ok"

    call = json.loads((tmp_path / "fake-hermes-call.json").read_text())
    assert call["env"] == {
        "DEN_TASK_ID": "1368",
        "DEN_RUN_ID": "run-123",
        "DEN_WORKER_ROLE": "coder",
        "DEN_EXPECTED_ARTIFACT": str(artifact_path),
    }
    assert call["cwd"] == str(tmp_path)
    assert Path(call["argv"][0]).name == "hermes"
    assert "chat" in call["argv"]
    assert "--provider" in call["argv"]
    assert "openrouter" in call["argv"]
    assert "--model" in call["argv"]
    assert "anthropic/claude-sonnet-4" in call["argv"]
    assert "--profile" in call["argv"]
    assert "den-coder" in call["argv"]
    assert "--toolsets" in call["argv"]
    assert "terminal,file,mcp" in call["argv"]
    assert "--source" in call["argv"]
    assert "den-worker" in call["argv"]
    assert "-q" in call["argv"]
    assert "EXPECTED COMPLETION ARTIFACT" in call["argv"][call["argv"].index("-q") + 1]


def test_spawned_hermes_worker_missing_artifact_is_incomplete(tmp_path):
    result = run_hermes_worker(
        task_id=1368,
        run_id="run-123",
        role="coder",
        prompt="Implement task 1368.",
        expected_artifact=tmp_path / ".den" / "runs" / "run-123" / "completion.json",
        cwd=tmp_path,
        env_overrides=fake_env(tmp_path, mode="missing_artifact"),
    )

    assert result.status == "incomplete"
    assert result.exit_code == 0
    assert result.artifact is None
    assert "missing completion artifact" in result.error.lower()


def test_spawned_hermes_worker_nonzero_exit_is_failed(tmp_path):
    result = run_hermes_worker(
        task_id=1368,
        run_id="run-123",
        role="reviewer",
        prompt="Review task 1368.",
        expected_artifact=tmp_path / ".den" / "runs" / "run-123" / "completion.json",
        cwd=tmp_path,
        env_overrides=fake_env(tmp_path, mode="nonzero"),
    )

    assert result.status == "failed"
    assert result.exit_code == 42
    assert "fake hermes failed" in result.stderr


def test_spawned_hermes_worker_bad_artifact_json_is_failed(tmp_path):
    result = run_hermes_worker(
        task_id=1368,
        run_id="run-123",
        role="coder",
        prompt="Implement task 1368.",
        expected_artifact=tmp_path / ".den" / "runs" / "run-123" / "completion.json",
        cwd=tmp_path,
        env_overrides=fake_env(tmp_path, mode="bad_json"),
    )

    assert result.status == "failed"
    assert result.exit_code == 0
    assert result.artifact is None
    assert "invalid completion artifact" in result.error.lower()


def test_spawned_hermes_worker_rejects_mismatched_identity_artifact(tmp_path):
    artifact_path = tmp_path / ".den" / "runs" / "run-123" / "completion.json"
    result = run_hermes_worker(
        task_id=1368,
        run_id="run-123",
        role="validator",
        prompt="Validate task 1368.",
        expected_artifact=artifact_path,
        cwd=tmp_path,
        env_overrides=fake_env(tmp_path, mode="identity_mismatch"),
    )

    assert result.status == "failed"
    assert result.artifact is None
    assert "role mismatch" in result.error.lower()

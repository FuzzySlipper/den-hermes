import json
import os
from pathlib import Path

from den_hermes.worker_launcher import run_coder_reviewer_sequence, run_hermes_worker


FAKE_HEAD = "0123456789abcdef0123456789abcdef01234567"


def install_fake_hermes(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    hermes = bin_dir / "hermes"
    hermes.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

log_path = Path(os.environ["FAKE_HERMES_LOG"])
artifact_path = Path(os.environ["DEN_EXPECTED_ARTIFACT"])
role = os.environ["DEN_WORKER_ROLE"]

log_path.parent.mkdir(parents=True, exist_ok=True)
entry = {
    "argv": sys.argv,
    "cwd": os.getcwd(),
    "env": {
        "DEN_TASK_ID": os.environ.get("DEN_TASK_ID"),
        "DEN_RUN_ID": os.environ.get("DEN_RUN_ID"),
        "DEN_WORKER_ROLE": role,
        "DEN_EXPECTED_ARTIFACT": os.environ.get("DEN_EXPECTED_ARTIFACT"),
    },
}
with log_path.open("a") as log_file:
    log_file.write(json.dumps(entry) + "\\n")

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
artifact_role = role
if mode == "identity_mismatch":
    artifact_role = "coder" if artifact_role != "coder" else "reviewer"

artifact_path.parent.mkdir(parents=True, exist_ok=True)
if role == "reviewer":
    artifact = {
        "task_id": artifact_task_id,
        "run_id": artifact_run_id,
        "role": artifact_role,
        "status": "completed",
        "verdict": "looks_good",
        "findings": [],
        "summary": "fake reviewer approved",
    }
else:
    artifact = {
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
    }
if mode == "missing_head_commit":
    artifact.pop("head_commit", None)
artifact_path.write_text(json.dumps(artifact, indent=2))
print(f"fake hermes {role} ok")
raise SystemExit(0)
"""
    )
    hermes.chmod(0o755)
    return bin_dir


def fake_env(tmp_path: Path, mode: str = "success") -> dict[str, str]:
    bin_dir = install_fake_hermes(tmp_path)
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_HERMES_LOG": str(tmp_path / "fake-hermes-call.jsonl"),
    }
    if mode != "success":
        env["FAKE_HERMES_MODE"] = mode
    return env


def read_fake_calls(tmp_path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (tmp_path / "fake-hermes-call.jsonl").read_text().splitlines()
        if line.strip()
    ]


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
    assert result.stdout.strip() == "fake hermes coder ok"

    call = read_fake_calls(tmp_path)[0]
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


def test_coder_artifact_requires_branch_head_commit_and_tests(tmp_path):
    result = run_hermes_worker(
        task_id=1368,
        run_id="coder-run",
        role="coder",
        prompt="Implement task 1368.",
        expected_artifact=tmp_path / ".den" / "runs" / "coder-run" / "completion.json",
        cwd=tmp_path,
        env_overrides=fake_env(tmp_path, mode="missing_head_commit"),
    )

    assert result.status == "failed"
    assert result.artifact is None
    assert "head_commit" in result.error


def test_fake_coder_then_reviewer_sequence_uses_distinct_runtime_args(tmp_path):
    result = run_coder_reviewer_sequence(
        task_id=1368,
        prompt="Use the Den task context to implement and review task 1368.",
        run_root=tmp_path / ".den" / "runs",
        cwd=tmp_path,
        coder={
            "run_id": "coder-run",
            "profile": "den-coder",
            "provider": "openrouter",
            "model": "anthropic/claude-sonnet-4",
            "toolsets": ["terminal", "file", "mcp"],
        },
        reviewer={
            "run_id": "reviewer-run",
            "profile": "den-reviewer",
            "provider": "openai-codex",
            "model": "gpt-5.1-codex",
            "toolsets": ["terminal", "file"],
        },
        env_overrides=fake_env(tmp_path),
    )

    assert result.status == "completed"
    assert result.coder.artifact["branch"] == "task/1368-fake"
    assert result.reviewer.artifact["verdict"] == "looks_good"

    calls = read_fake_calls(tmp_path)
    assert [call["env"]["DEN_WORKER_ROLE"] for call in calls] == ["coder", "reviewer"]
    assert [call["env"]["DEN_RUN_ID"] for call in calls] == ["coder-run", "reviewer-run"]
    coder_args = calls[0]["argv"]
    reviewer_args = calls[1]["argv"]
    assert "den-coder" in coder_args
    assert "openrouter" in coder_args
    assert "anthropic/claude-sonnet-4" in coder_args
    assert "terminal,file,mcp" in coder_args
    assert "den-reviewer" in reviewer_args
    assert "openai-codex" in reviewer_args
    assert "gpt-5.1-codex" in reviewer_args
    assert "terminal,file" in reviewer_args
    assert "task/1368-fake" in reviewer_args[reviewer_args.index("-q") + 1]
    assert FAKE_HEAD in reviewer_args[reviewer_args.index("-q") + 1]

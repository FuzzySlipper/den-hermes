import json
import os
import subprocess
from pathlib import Path

import pytest

from den_hermes.worker_launcher import (
    run_coder_reviewer_sequence,
    run_den_coder_reviewer_workflow,
    run_hermes_worker,
)


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
        "DEN_PROJECT_ID": os.environ.get("DEN_PROJECT_ID"),
        "DEN_CHANNELS_ACTIVITY_CONTEXT": os.environ.get("DEN_CHANNELS_ACTIVITY_CONTEXT"),
    },
}
with log_path.open("a") as log_file:
    if entry["env"].get("DEN_PROJECT_ID") is None:
        entry["env"].pop("DEN_PROJECT_ID", None)
    if entry["env"].get("DEN_CHANNELS_ACTIVITY_CONTEXT") is None:
        entry["env"].pop("DEN_CHANNELS_ACTIVITY_CONTEXT", None)
    log_file.write(json.dumps(entry) + "\\n")

mode = os.environ.get(f"FAKE_HERMES_{role.upper()}_MODE", os.environ.get("FAKE_HERMES_MODE", "success"))

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
artifact_project_id = os.environ.get("DEN_PROJECT_ID")
if mode == "identity_mismatch":
    artifact_role = "coder" if artifact_role != "coder" else "reviewer"
if mode == "role_alias":
    artifact_role = f"spawned-{role}"
if mode == "string_task_id":
    artifact_task_id = os.environ["DEN_TASK_ID"]
if mode == "wrong_project_id":
    artifact_project_id = "den-hermes"

artifact_path.parent.mkdir(parents=True, exist_ok=True)
if role == "reviewer":
    findings = json.loads(os.environ.get("FAKE_REVIEW_FINDINGS", "[]"))
    tests_run = json.loads(os.environ.get("FAKE_REVIEW_TESTS_RUN", "[]"))
    if mode == "failed_review_checks":
        tests_run = [{"command": "git diff --check", "result": "exit 2: whitespace errors"}]
    artifact = {
        "task_id": artifact_task_id,
        "run_id": artifact_run_id,
        "role": artifact_role,
        "status": "completed",
        "verdict": os.environ.get("FAKE_REVIEW_VERDICT", "looks_good"),
        "findings": findings,
        "summary": "fake reviewer approved",
    }
    if tests_run:
        artifact["tests_run"] = tests_run
elif role == "validator":
    artifact = {
        "task_id": artifact_task_id,
        "run_id": artifact_run_id,
        "role": artifact_role,
        "status": "completed",
        "verdict": os.environ.get("FAKE_VALIDATOR_VERDICT", "passed"),
        "tests_run": json.loads(os.environ.get("FAKE_VALIDATOR_TESTS_RUN", '[{"command": "pytest tests/ -q", "result": "passed"}]')),
        "summary": "fake validator passed",
    }
elif role == "drift_checker":
    artifact = {
        "task_id": artifact_task_id,
        "run_id": artifact_run_id,
        "role": artifact_role,
        "status": "completed",
        "verdict": os.environ.get("FAKE_DRIFT_VERDICT", "passed"),
        "checked_refs": json.loads(os.environ.get("FAKE_DRIFT_CHECKED_REFS", '["main", "task/1399-gates"]')),
        "notes": "fake drift check passed",
        "summary": os.environ.get("FAKE_DRIFT_SUMMARY", "fake drift checker passed"),
    }
elif role == "packet_auditor":
    artifact = {
        "task_id": artifact_task_id,
        "run_id": artifact_run_id,
        "role": artifact_role,
        "status": "completed",
        "verdict": os.environ.get("FAKE_AUDIT_VERDICT", "passed"),
        "audited_packets": json.loads(os.environ.get("FAKE_AUDITED_PACKETS", '[5793]')),
        "notes": "fake packet audit passed",
        "summary": os.environ.get("FAKE_AUDIT_SUMMARY", "fake packet auditor passed"),
    }
else:
    fake_branch = os.environ.get("FAKE_BRANCH", "task/1368-fake")
    fake_head = os.environ.get("FAKE_HEAD", "0123456789abcdef0123456789abcdef01234567")
    claimed_finding_ids = json.loads(os.environ.get("FAKE_CLAIMED_FINDING_IDS", "[]"))
    artifact = {
        "task_id": artifact_task_id,
        "run_id": artifact_run_id,
        "role": artifact_role,
        "status": "completed",
        "branch": fake_branch,
        "head_commit": fake_head,
        "tests_run": [
            {"command": "pytest tests/ -q", "result": "passed"}
        ],
        "summary": "fake worker completed",
    }
    if claimed_finding_ids:
        artifact["claimed_finding_ids"] = claimed_finding_ids
        artifact["response_notes"] = os.environ.get("FAKE_RESPONSE_NOTES", "fake coder claims findings fixed")
if mode == "missing_head_commit":
    artifact.pop("head_commit", None)
if mode == "missing_summary":
    artifact.pop("summary", None)
if artifact_project_id is not None:
    artifact["project_id"] = artifact_project_id
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


def init_git_repo(tmp_path: Path) -> str:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("# test\n")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True, text=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True)
    return head.stdout.strip()


def read_fake_calls(tmp_path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (tmp_path / "fake-hermes-call.jsonl").read_text().splitlines()
        if line.strip()
    ]


def write_runtime_registry(tmp_path: Path, *, coder_model="model-coder", reviewer_model="model-reviewer") -> Path:
    registry = tmp_path / "runtime-registry.yaml"
    registry.write_text(
        f"""
schema_version: 1
registry_id: test-launcher-registry
defaults:
  substrate: spawned_hermes
  hermes_binary: hermes
  run_root: {tmp_path / 'runs'}
  artifact_filename: completion.json
  log_filename: worker.log
  profile_required: true
  provider_required: true
  model_required: true
  timeout_seconds: 600
  toolsets: [file]
  workdir: {tmp_path}
roles:
  coder:
    runtime_id: coder-runtime
    profile: den-coder-profile
    provider: provider-coder
    model: {coder_model}
    toolsets: [terminal, file]
    timeout_seconds: 901
  reviewer:
    runtime_id: reviewer-runtime
    profile: den-reviewer-profile
    provider: provider-reviewer
    model: {reviewer_model}
    toolsets: [file]
    timeout_seconds: 902
  validator:
    runtime_id: validator-runtime
    profile: den-validator-profile
    provider: provider-validator
    model: model-validator
  drift_checker:
    runtime_id: drift-runtime
    profile: den-drift-profile
    provider: provider-drift
    model: model-drift
  packet_auditor:
    runtime_id: audit-runtime
    profile: den-audit-profile
    provider: provider-audit
    model: model-audit
  project_orchestrator:
    runtime_id: project-orchestrator-runtime
    profile: spawned-orchestrator
    provider: provider-orchestrator
    model: model-orchestrator
    toolsets: [terminal, file]
    timeout_seconds: 903
    launch:
      source: den-project-orchestrator
      extra_args: []
    lease_kind: project_orchestrator
role_aliases:
  orchestrator: project_orchestrator
  pooled_orchestrator: project_orchestrator
"""
    )
    return registry


class RecordingDenClient:
    def __init__(
        self,
        launch_log: Path | None = None,
        fail_registration_roles: set[str] | None = None,
        fail_completion_roles: set[str] | None = None,
        project_id: str = "den-hermes-bridge",
    ):
        self.events = []
        self.project_id = project_id
        self.launch_log = launch_log
        self.fail_registration_roles = fail_registration_roles or set()
        self.fail_completion_roles = fail_completion_roles or set()

    def register_worker_run(self, **kwargs):
        if kwargs["role"] in self.fail_registration_roles:
            raise RuntimeError(f"registration failed for {kwargs['role']}")
        if self.launch_log is not None and kwargs["role"] == "coder":
            assert not self.launch_log.exists(), "worker was launched before Den registration"
        self.events.append(("registered", kwargs["task_id"], kwargs["run_id"], kwargs["role"], kwargs))
        return {"worker_run": {"run_id": kwargs["run_id"]}}

    def mark_worker_started(self, *, task_id, run_id, role):
        self.events.append(("started", task_id, run_id, role))

    def mark_worker_completed(self, *, task_id, run_id, role, artifact):
        if role in self.fail_completion_roles:
            raise RuntimeError(f"completion rejected for {role}")
        self.events.append(("completed", task_id, run_id, role, artifact))

    def mark_worker_failed(self, *, task_id, run_id, role, error):
        self.events.append(("failed", task_id, run_id, role, error))

    def request_review(self, *, task_id, branch, head_commit, tests_run, coder_run_id):
        self.events.append(("review_requested", task_id, branch, head_commit, tests_run, coder_run_id))
        return {"review_round_id": 321}

    def post_review_findings(self, *, task_id, review_request, reviewer_run_id, verdict, findings, summary):
        self.events.append(
            ("review_findings_posted", task_id, review_request, reviewer_run_id, verdict, findings, summary)
        )


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


def test_spawned_hermes_worker_injects_activity_context_and_preserves_identity(tmp_path):
    artifact_path = tmp_path / ".den" / "runs" / "run-activity" / "completion.json"
    env = fake_env(tmp_path)
    env.update({
        "DEN_RUN_ID": "wrong-run",
        "DEN_WORKER_ROLE": "wrong-role",
        "DEN_CHANNELS_ACTIVITY_CONTEXT": "",
    })

    result = run_hermes_worker(
        task_id=1565,
        run_id="run-activity",
        role="coder",
        prompt="Implement task 1565.",
        expected_artifact=artifact_path,
        cwd=tmp_path,
        env_overrides=env,
        activity_context={
            "gatewayUrl": "http://gateway.test",
            "channelId": 42,
            "projectId": "den-hermes-bridge",
            "displayBlockId": "block-701",
            "parentHermesSessionKey": "parent-session",
            "parentAgentIdentity": "den-mcp-runner",
            "agentIdentity": "den-coder-profile",
            "token": "secret-token",
        },
    )

    assert result.status == "completed"
    call = read_fake_calls(tmp_path)[0]
    assert call["env"]["DEN_RUN_ID"] == "run-activity"
    assert call["env"]["DEN_WORKER_ROLE"] == "coder"
    activity_context = json.loads(call["env"]["DEN_CHANNELS_ACTIVITY_CONTEXT"])
    assert activity_context["displayBlockId"] == "block-701"
    assert activity_context["workerRunId"] == "run-activity"
    assert activity_context["workerRole"] == "coder"
    assert activity_context["token"] == "secret-token"


def test_spawned_hermes_worker_without_activity_context_does_not_inject_env(tmp_path):
    result = run_hermes_worker(
        task_id=1565,
        run_id="run-no-activity",
        role="coder",
        prompt="Implement task 1565.",
        expected_artifact=tmp_path / ".den" / "runs" / "run-no-activity" / "completion.json",
        cwd=tmp_path,
        env_overrides=fake_env(tmp_path),
    )

    assert result.status == "completed"
    assert "DEN_CHANNELS_ACTIVITY_CONTEXT" not in read_fake_calls(tmp_path)[0]["env"]


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
    assert "identity mismatch" in result.error.lower()
    assert "role" in result.error.lower()


def test_spawned_hermes_worker_reports_project_id_mismatch(tmp_path):
    result = run_hermes_worker(
        task_id=1368,
        run_id="run-123",
        role="coder",
        project_id="den-hermes-bridge",
        prompt="Implement task 1368.",
        expected_artifact=tmp_path / ".den" / "runs" / "run-123" / "completion.json",
        cwd=tmp_path,
        env_overrides=fake_env(tmp_path, mode="wrong_project_id"),
    )

    assert result.status == "failed"
    assert result.artifact is None
    assert "project_id" in result.error
    assert "den-hermes-bridge" in result.error
    assert "den-hermes" in result.error


def test_spawned_hermes_worker_rejects_string_task_id(tmp_path):
    result = run_hermes_worker(
        task_id=1368,
        run_id="run-123",
        role="coder",
        prompt="Implement task 1368.",
        expected_artifact=tmp_path / ".den" / "runs" / "run-123" / "completion.json",
        cwd=tmp_path,
        env_overrides=fake_env(tmp_path, mode="string_task_id"),
    )

    assert result.status == "failed"
    assert result.artifact is None
    assert "task_id expected 1368 (int)" in result.error
    assert "'1368'" in result.error


def test_spawned_hermes_worker_rejects_role_alias(tmp_path):
    result = run_hermes_worker(
        task_id=1368,
        run_id="run-123",
        role="reviewer",
        prompt="Review task 1368.",
        expected_artifact=tmp_path / ".den" / "runs" / "run-123" / "completion.json",
        cwd=tmp_path,
        env_overrides=fake_env(tmp_path, mode="role_alias"),
    )

    assert result.status == "failed"
    assert result.artifact is None
    assert "role expected 'reviewer'" in result.error
    assert "spawned-reviewer" in result.error


def test_spawned_hermes_worker_reports_missing_summary_before_publication(tmp_path):
    result = run_hermes_worker(
        task_id=1368,
        run_id="run-123",
        role="coder",
        prompt="Implement task 1368.",
        expected_artifact=tmp_path / ".den" / "runs" / "run-123" / "completion.json",
        cwd=tmp_path,
        env_overrides=fake_env(tmp_path, mode="missing_summary"),
    )

    assert result.status == "failed"
    assert result.artifact is None
    assert "summary" in result.error


def test_reviewer_artifact_cannot_approve_failed_required_checks(tmp_path):
    result = run_hermes_worker(
        task_id=1368,
        run_id="run-123",
        role="reviewer",
        prompt="Review task 1368.",
        expected_artifact=tmp_path / ".den" / "runs" / "run-123" / "completion.json",
        cwd=tmp_path,
        env_overrides=fake_env(tmp_path, mode="failed_review_checks"),
    )

    assert result.status == "failed"
    assert result.artifact is None
    assert "looks_good" in (result.error or "")
    assert "failure" in (result.error or "") or "non-zero" in (result.error or "")


def test_reviewer_artifact_cannot_approve_mixed_failed_and_passed_summary(tmp_path):
    env = fake_env(tmp_path)
    env["FAKE_REVIEW_TESTS_RUN"] = '[{"command":"pytest -q","result":"1 failed, 2 passed"}]'

    result = run_hermes_worker(
        task_id=1368,
        run_id="run-123",
        role="reviewer",
        prompt="Review task 1368.",
        expected_artifact=tmp_path / ".den" / "runs" / "run-123" / "completion.json",
        cwd=tmp_path,
        env_overrides=env,
    )

    assert result.status == "failed"
    assert result.artifact is None
    assert "looks_good" in (result.error or "")


def test_reviewer_artifact_allows_dotnet_success_summaries(tmp_path):
    """Regression: .NET '0 Error(s)' and 'Failed: 0' should not block looks_good."""
    env = fake_env(tmp_path)
    env["FAKE_REVIEW_TESTS_RUN"] = json.dumps([
        "dotnet build: Build succeeded. 0 Warning(s), 0 Error(s)",
        "Passed: 12, Failed: 0",
    ])

    result = run_hermes_worker(
        task_id=1368,
        run_id="run-net-success",
        role="reviewer",
        prompt="Review .NET build output.",
        expected_artifact=tmp_path / ".den" / "runs" / "run-net-success" / "completion.json",
        cwd=tmp_path,
        env_overrides=env,
    )

    assert result.status == "completed", (
        f"Expected completed, got status={result.status} error={result.error}"
    )
    assert result.artifact is not None
    assert result.artifact.get("verdict") == "looks_good"


def test_reviewer_artifact_rejects_word_first_failure(tmp_path):
    """Word-first 'Failed: 2' should still be rejected as real failure."""
    env = fake_env(tmp_path)
    env["FAKE_REVIEW_TESTS_RUN"] = json.dumps([
        "Passed: 5, Failed: 2",
    ])

    result = run_hermes_worker(
        task_id=1368,
        run_id="run-fail-word-first",
        role="reviewer",
        prompt="Review with failures.",
        expected_artifact=tmp_path / ".den" / "runs" / "run-fail-word-first" / "completion.json",
        cwd=tmp_path,
        env_overrides=env,
    )

    assert result.status == "failed"
    assert result.artifact is None
    assert "looks_good" in (result.error or "")


def test_reviewer_artifact_allows_dict_form_zero_failure(tmp_path):
    """Regression: dict-form '{"failed": 0, "passed": 67}' should not block looks_good."""
    env = fake_env(tmp_path)
    env["FAKE_REVIEW_TESTS_RUN"] = json.dumps([
        {"passed": 67, "failed": 0},
    ])

    result = run_hermes_worker(
        task_id=1368,
        run_id="run-dict-zero",
        role="reviewer",
        prompt="Review with dict-form zero failures.",
        expected_artifact=tmp_path / ".den" / "runs" / "run-dict-zero" / "completion.json",
        cwd=tmp_path,
        env_overrides=env,
    )

    assert result.status == "completed", (
        f"Expected completed, got status={result.status} error={result.error}"
    )
    assert result.artifact is not None
    assert result.artifact.get("verdict") == "looks_good"


def test_reviewer_artifact_rejects_dict_form_nonzero_failure(tmp_path):
    """Dict-form '{"failed": 1, "passed": 2}' should still block looks_good."""
    env = fake_env(tmp_path)
    env["FAKE_REVIEW_TESTS_RUN"] = json.dumps([
        {"failed": 1, "passed": 2},
    ])

    result = run_hermes_worker(
        task_id=1368,
        run_id="run-dict-nonzero",
        role="reviewer",
        prompt="Review with dict-form real failure.",
        expected_artifact=tmp_path / ".den" / "runs" / "run-dict-nonzero" / "completion.json",
        cwd=tmp_path,
        env_overrides=env,
    )

    assert result.status == "failed"
    assert result.artifact is None
    assert "looks_good" in (result.error or "")
    assert "tests_run" in (result.error or "")


def test_reviewer_artifact_allows_shaped_1641_zero_failure(tmp_path):
    """Synthetic #1641 rereview shape: structured tests_run with dict entries
    that contain zero-failure counters like '{"failed": 0, "total": 67}'."""
    env = fake_env(tmp_path)
    env["FAKE_REVIEW_TESTS_RUN"] = json.dumps([
        {"command": "git diff --check", "exit_code": 0, "result": "no whitespace errors"},
        {"command": "python -m pytest -q", "exit_code": 0, "result": {"passed": 67, "failed": 0}},
    ])

    result = run_hermes_worker(
        task_id=1368,
        run_id="run-1641-shape",
        role="reviewer",
        prompt="Review #1641 work.",
        expected_artifact=tmp_path / ".den" / "runs" / "run-1641-shape" / "completion.json",
        cwd=tmp_path,
        env_overrides=env,
    )

    assert result.status == "completed", (
        f"Expected completed, got status={result.status} error={result.error}"
    )
    assert result.artifact is not None
    assert result.artifact.get("verdict") == "looks_good"


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



def test_validator_artifact_requires_validation_evidence(tmp_path):
    env = fake_env(tmp_path)
    env["FAKE_VALIDATOR_TESTS_RUN"] = "[]"

    result = run_hermes_worker(
        task_id=1368,
        run_id="validator-run",
        role="validator",
        prompt="Validate task 1368.",
        expected_artifact=tmp_path / ".den" / "runs" / "validator-run" / "completion.json",
        cwd=tmp_path,
        env_overrides=env,
    )

    assert result.status == "failed"
    assert result.artifact is None
    assert "validation evidence" in result.error


def test_drift_checker_artifact_requires_checked_refs_or_packets(tmp_path):
    env = fake_env(tmp_path)
    env["FAKE_DRIFT_CHECKED_REFS"] = "[]"

    result = run_hermes_worker(
        task_id=1368,
        run_id="drift-run",
        role="drift_checker",
        prompt="Check drift for task 1368.",
        expected_artifact=tmp_path / ".den" / "runs" / "drift-run" / "completion.json",
        cwd=tmp_path,
        env_overrides=env,
    )

    assert result.status == "failed"
    assert result.artifact is None
    assert "checked_refs" in result.error


def test_packet_auditor_artifact_requires_audited_packets(tmp_path):
    env = fake_env(tmp_path)
    env["FAKE_AUDITED_PACKETS"] = "[]"

    result = run_hermes_worker(
        task_id=1368,
        run_id="audit-run",
        role="packet_auditor",
        prompt="Audit packets for task 1368.",
        expected_artifact=tmp_path / ".den" / "runs" / "audit-run" / "completion.json",
        cwd=tmp_path,
        env_overrides=env,
    )

    assert result.status == "failed"
    assert result.artifact is None
    assert "audited_packets" in result.error


@pytest.mark.parametrize(
    ("role", "env_key"),
    [
        ("validator", "FAKE_VALIDATOR_VERDICT"),
        ("drift_checker", "FAKE_DRIFT_VERDICT"),
        ("packet_auditor", "FAKE_AUDIT_VERDICT"),
    ],
)
def test_gate_role_artifact_rejects_non_passing_verdict(tmp_path, role, env_key):
    env = fake_env(tmp_path)
    env[env_key] = "failed"

    result = run_hermes_worker(
        task_id=1368,
        run_id=f"{role}-run",
        role=role,
        prompt=f"Run {role} gate.",
        expected_artifact=tmp_path / ".den" / "runs" / f"{role}-run" / "completion.json",
        cwd=tmp_path,
        env_overrides=env,
    )

    assert result.status == "failed"
    assert result.artifact is None
    assert "verdict" in result.error


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


def test_sequence_git_verification_blocks_reviewer_when_branch_is_missing(tmp_path):
    init_git_repo(tmp_path)

    result = run_coder_reviewer_sequence(
        task_id=1368,
        prompt="Use the Den task context to implement and review task 1368.",
        run_root=tmp_path / ".den" / "runs",
        cwd=tmp_path,
        verify_git=True,
        coder={"run_id": "coder-run"},
        reviewer={"run_id": "reviewer-run"},
        env_overrides=fake_env(tmp_path),
    )

    assert result.status == "failed"
    assert result.reviewer is None
    assert "branch" in result.error.lower()
    assert [call["env"]["DEN_WORKER_ROLE"] for call in read_fake_calls(tmp_path)] == ["coder"]


def test_sequence_git_verification_allows_reviewer_when_branch_head_resolve(tmp_path):
    head = init_git_repo(tmp_path)
    subprocess.run(["git", "checkout", "-b", "task/1368-fake"], cwd=tmp_path, check=True, capture_output=True, text=True)
    env = fake_env(tmp_path)
    env["FAKE_HEAD"] = head

    result = run_coder_reviewer_sequence(
        task_id=1368,
        prompt="Use the Den task context to implement and review task 1368.",
        run_root=tmp_path / ".den" / "runs",
        cwd=tmp_path,
        verify_git=True,
        coder={"run_id": "coder-run"},
        reviewer={"run_id": "reviewer-run"},
        env_overrides=env,
    )

    assert result.status == "completed"
    assert result.coder.artifact["head_commit"] == head
    assert [call["env"]["DEN_WORKER_ROLE"] for call in read_fake_calls(tmp_path)] == ["coder", "reviewer"]


def test_den_workflow_uses_resolved_runtime_for_coder_and_reviewer(tmp_path):
    head = init_git_repo(tmp_path)
    subprocess.run(["git", "checkout", "-b", "task/1368-fake"], cwd=tmp_path, check=True, capture_output=True, text=True)
    env = fake_env(tmp_path)
    env["FAKE_HEAD"] = head
    den = RecordingDenClient()
    registry = write_runtime_registry(tmp_path)

    result = run_den_coder_reviewer_workflow(
        den_client=den,
        task_id=1368,
        prompt="Use the Den task context to implement and review task 1368.",
        run_root=tmp_path / ".den" / "runs",
        cwd=tmp_path,
        verify_git=True,
        coder={"run_id": "coder-run"},
        reviewer={"run_id": "reviewer-run"},
        runtime_registry_path=registry,
        env_overrides=env,
    )

    assert result.status == "completed"
    coder_registration = den.events[0][4]
    reviewer_registration = den.events[4][4]
    assert coder_registration["profile"] == "den-coder-profile"
    assert coder_registration["provider"] == "provider-coder"
    assert coder_registration["model"] == "model-coder"
    assert coder_registration["toolsets"] == ["terminal", "file"]
    assert coder_registration["timeout_seconds"] == 901
    assert coder_registration["runtime_id"] == "coder-runtime"
    assert reviewer_registration["profile"] == "den-reviewer-profile"
    assert reviewer_registration["provider"] == "provider-reviewer"
    assert reviewer_registration["model"] == "model-reviewer"
    assert reviewer_registration["runtime_id"] == "reviewer-runtime"
    calls = read_fake_calls(tmp_path)
    assert calls[0]["argv"][calls[0]["argv"].index("--profile") + 1] == "den-coder-profile"
    assert calls[0]["argv"][calls[0]["argv"].index("--model") + 1] == "model-coder"
    assert calls[1]["argv"][calls[1]["argv"].index("--profile") + 1] == "den-reviewer-profile"
    assert calls[1]["argv"][calls[1]["argv"].index("--model") + 1] == "model-reviewer"


def test_den_workflow_central_runtime_change_changes_launch_args(tmp_path):
    head = init_git_repo(tmp_path)
    subprocess.run(["git", "checkout", "-b", "task/1368-fake"], cwd=tmp_path, check=True, capture_output=True, text=True)
    env = fake_env(tmp_path)
    env["FAKE_HEAD"] = head
    registry = write_runtime_registry(tmp_path, coder_model="model-coder-v2", reviewer_model="model-reviewer-v2")

    result = run_den_coder_reviewer_workflow(
        den_client=RecordingDenClient(),
        task_id=1368,
        prompt="Use the Den task context to implement and review task 1368.",
        run_root=tmp_path / ".den" / "runs",
        cwd=tmp_path,
        verify_git=True,
        coder={"run_id": "coder-run"},
        reviewer={"run_id": "reviewer-run"},
        runtime_registry_path=registry,
        env_overrides=env,
    )

    assert result.status == "completed"
    calls = read_fake_calls(tmp_path)
    assert calls[0]["argv"][calls[0]["argv"].index("--model") + 1] == "model-coder-v2"
    assert calls[1]["argv"][calls[1]["argv"].index("--model") + 1] == "model-reviewer-v2"


def test_den_workflow_resolver_failure_prevents_worker_launch(tmp_path):
    registry = write_runtime_registry(tmp_path)
    registry.write_text(registry.read_text().replace("    profile: den-coder-profile\n", "", 1))
    den = RecordingDenClient()

    result = run_den_coder_reviewer_workflow(
        den_client=den,
        task_id=1368,
        prompt="Use the Den task context to implement and review task 1368.",
        run_root=tmp_path / ".den" / "runs",
        cwd=tmp_path,
        coder={"run_id": "coder-run"},
        reviewer={"run_id": "reviewer-run"},
        runtime_registry_path=registry,
        env_overrides=fake_env(tmp_path),
    )

    assert result.status == "failed"
    assert "runtime resolver failed" in result.error.lower()
    assert den.events == []
    assert not (tmp_path / "fake-hermes-call.jsonl").exists()


def test_den_workflow_rejects_hidden_runtime_override_when_registry_enabled(tmp_path):
    den = RecordingDenClient()

    result = run_den_coder_reviewer_workflow(
        den_client=den,
        task_id=1368,
        prompt="Use the Den task context to implement and review task 1368.",
        run_root=tmp_path / ".den" / "runs",
        cwd=tmp_path,
        coder={"run_id": "coder-run", "provider": "hidden-provider"},
        reviewer={"run_id": "reviewer-run"},
        runtime_registry_path=write_runtime_registry(tmp_path),
        env_overrides=fake_env(tmp_path),
    )

    assert result.status == "failed"
    assert "allow_runtime_override" in result.error
    assert den.events == []
    assert not (tmp_path / "fake-hermes-call.jsonl").exists()


def test_den_workflow_registers_coder_before_launching_worker(tmp_path):
    head = init_git_repo(tmp_path)
    subprocess.run(["git", "checkout", "-b", "task/1368-fake"], cwd=tmp_path, check=True, capture_output=True, text=True)
    env = fake_env(tmp_path)
    env["FAKE_HEAD"] = head
    den = RecordingDenClient(launch_log=tmp_path / "fake-hermes-call.jsonl")

    result = run_den_coder_reviewer_workflow(
        den_client=den,
        task_id=1368,
        prompt="Use the Den task context to implement and review task 1368.",
        run_root=tmp_path / ".den" / "runs",
        cwd=tmp_path,
        verify_git=True,
        coder={"run_id": "coder-run", "profile": "den-coder", "provider": "openrouter", "model": "model-a"},
        reviewer={"run_id": "reviewer-run"},
        env_overrides=env,
    )

    assert result.status == "completed"
    first_event = den.events[0]
    assert first_event[0:4] == ("registered", 1368, "coder-run", "coder")
    assert first_event[4]["artifact_path"] == str(tmp_path / ".den" / "runs" / "coder-run" / "completion.json")
    assert first_event[4]["profile"] == "den-coder"
    assert first_event[4]["provider"] == "openrouter"
    assert first_event[4]["model"] == "model-a"
    assert read_fake_calls(tmp_path)[0]["env"]["DEN_RUN_ID"] == "coder-run"


def test_den_workflow_registration_failure_prevents_worker_launch(tmp_path):
    den = RecordingDenClient(fail_registration_roles={"coder"})

    result = run_den_coder_reviewer_workflow(
        den_client=den,
        task_id=1368,
        prompt="Use the Den task context to implement and review task 1368.",
        run_root=tmp_path / ".den" / "runs",
        cwd=tmp_path,
        coder={"run_id": "coder-run"},
        reviewer={"run_id": "reviewer-run"},
        env_overrides=fake_env(tmp_path),
    )

    assert result.status == "failed"
    assert "registration failed" in result.error
    assert den.events == []
    assert not (tmp_path / "fake-hermes-call.jsonl").exists()


def test_den_workflow_coder_completion_rejection_stops_before_review(tmp_path):
    head = init_git_repo(tmp_path)
    subprocess.run(["git", "checkout", "-b", "task/1368-fake"], cwd=tmp_path, check=True, capture_output=True, text=True)
    env = fake_env(tmp_path)
    env["FAKE_HEAD"] = head
    den = RecordingDenClient(fail_completion_roles={"coder"})

    result = run_den_coder_reviewer_workflow(
        den_client=den,
        task_id=1368,
        prompt="Use the Den task context to implement and review task 1368.",
        run_root=tmp_path / ".den" / "runs",
        cwd=tmp_path,
        verify_git=True,
        coder={"run_id": "coder-run"},
        reviewer={"run_id": "reviewer-run"},
        env_overrides=env,
    )

    assert result.status == "failed"
    assert result.review_request is None
    assert "coder completion rejected" in result.error.lower()
    assert [event[0:4] for event in den.events] == [
        ("registered", 1368, "coder-run", "coder"),
        ("started", 1368, "coder-run", "coder"),
    ]
    assert [call["env"]["DEN_WORKER_ROLE"] for call in read_fake_calls(tmp_path)] == ["coder"]


def test_den_workflow_reviewer_registration_failure_prevents_reviewer_launch(tmp_path):
    head = init_git_repo(tmp_path)
    subprocess.run(["git", "checkout", "-b", "task/1368-fake"], cwd=tmp_path, check=True, capture_output=True, text=True)
    env = fake_env(tmp_path)
    env["FAKE_HEAD"] = head
    den = RecordingDenClient(fail_registration_roles={"reviewer"})

    result = run_den_coder_reviewer_workflow(
        den_client=den,
        task_id=1368,
        prompt="Use the Den task context to implement and review task 1368.",
        run_root=tmp_path / ".den" / "runs",
        cwd=tmp_path,
        verify_git=True,
        coder={"run_id": "coder-run"},
        reviewer={"run_id": "reviewer-run"},
        env_overrides=env,
    )

    assert result.status == "failed"
    assert "reviewer worker registration failed" in result.error.lower()
    assert [event[0:4] for event in den.events] == [
        ("registered", 1368, "coder-run", "coder"),
        ("started", 1368, "coder-run", "coder"),
        ("completed", 1368, "coder-run", "coder"),
        ("review_requested", 1368, "task/1368-fake", head),
    ]
    assert [call["env"]["DEN_WORKER_ROLE"] for call in read_fake_calls(tmp_path)] == ["coder"]


def test_den_workflow_records_status_and_requests_review_after_verified_coder(tmp_path):
    head = init_git_repo(tmp_path)
    subprocess.run(["git", "checkout", "-b", "task/1368-fake"], cwd=tmp_path, check=True, capture_output=True, text=True)
    env = fake_env(tmp_path)
    env["FAKE_HEAD"] = head
    den = RecordingDenClient()

    result = run_den_coder_reviewer_workflow(
        den_client=den,
        task_id=1368,
        prompt="Use the Den task context to implement and review task 1368.",
        run_root=tmp_path / ".den" / "runs",
        cwd=tmp_path,
        verify_git=True,
        coder={"run_id": "coder-run"},
        reviewer={"run_id": "reviewer-run"},
        env_overrides=env,
    )

    assert result.status == "completed"
    assert result.review_request == {"review_round_id": 321}
    assert den.events[0][0:4] == ("registered", 1368, "coder-run", "coder")
    assert den.events[1] == ("started", 1368, "coder-run", "coder")
    assert den.events[2][0:4] == ("completed", 1368, "coder-run", "coder")
    assert den.events[3] == (
        "review_requested",
        1368,
        "task/1368-fake",
        head,
        [{"command": "pytest tests/ -q", "result": "passed"}],
        "coder-run",
    )
    assert den.events[4][0:4] == ("registered", 1368, "reviewer-run", "reviewer")
    assert den.events[5] == ("started", 1368, "reviewer-run", "reviewer")
    assert den.events[6][0:4] == ("completed", 1368, "reviewer-run", "reviewer")


def test_den_workflow_does_not_request_review_or_launch_reviewer_when_coder_git_fails(tmp_path):
    init_git_repo(tmp_path)
    den = RecordingDenClient()

    result = run_den_coder_reviewer_workflow(
        den_client=den,
        task_id=1368,
        prompt="Use the Den task context to implement and review task 1368.",
        run_root=tmp_path / ".den" / "runs",
        cwd=tmp_path,
        verify_git=True,
        coder={"run_id": "coder-run"},
        reviewer={"run_id": "reviewer-run"},
        env_overrides=fake_env(tmp_path),
    )

    assert result.status == "failed"
    assert result.review_request is None
    assert [event[0:4] for event in den.events] == [
        ("registered", 1368, "coder-run", "coder"),
        ("started", 1368, "coder-run", "coder"),
        ("completed", 1368, "coder-run", "coder"),
        ("failed", 1368, "coder-run", "coder"),
    ]
    assert "branch" in den.events[-1][-1].lower()
    assert [call["env"]["DEN_WORKER_ROLE"] for call in read_fake_calls(tmp_path)] == ["coder"]


def test_den_workflow_reviewer_artifact_failure_posts_failure_packet(tmp_path):
    head = init_git_repo(tmp_path)
    subprocess.run(["git", "checkout", "-b", "task/1368-fake"], cwd=tmp_path, check=True, capture_output=True, text=True)
    env = fake_env(tmp_path)
    env["FAKE_HEAD"] = head
    env["FAKE_HERMES_REVIEWER_MODE"] = "missing_artifact"
    den = RecordingDenClient()

    result = run_den_coder_reviewer_workflow(
        den_client=den,
        task_id=1368,
        prompt="Use the Den task context to implement and review task 1368.",
        run_root=tmp_path / ".den" / "runs",
        cwd=tmp_path,
        verify_git=True,
        coder={"run_id": "coder-run"},
        reviewer={"run_id": "reviewer-run"},
        env_overrides=env,
    )

    assert result.status == "failed"
    assert "missing completion artifact" in result.error.lower()
    assert den.events[-1][0:4] == ("failed", 1368, "reviewer-run", "reviewer")
    assert [call["env"]["DEN_WORKER_ROLE"] for call in read_fake_calls(tmp_path)] == ["coder", "reviewer"]


def test_den_workflow_reviewer_completion_rejection_skips_findings_packet(tmp_path):
    head = init_git_repo(tmp_path)
    subprocess.run(["git", "checkout", "-b", "task/1368-fake"], cwd=tmp_path, check=True, capture_output=True, text=True)
    env = fake_env(tmp_path)
    env["FAKE_HEAD"] = head
    den = RecordingDenClient(fail_completion_roles={"reviewer"})

    result = run_den_coder_reviewer_workflow(
        den_client=den,
        task_id=1368,
        prompt="Use the Den task context to implement and review task 1368.",
        run_root=tmp_path / ".den" / "runs",
        cwd=tmp_path,
        verify_git=True,
        coder={"run_id": "coder-run"},
        reviewer={"run_id": "reviewer-run"},
        env_overrides=env,
    )

    assert result.status == "failed"
    assert "reviewer completion rejected" in result.error.lower()
    assert not [event for event in den.events if event[0] == "review_findings_posted"]
    assert den.events[-1][0:4] == ("started", 1368, "reviewer-run", "reviewer")


def test_den_workflow_posts_reviewer_findings_after_reviewer_completion(tmp_path):
    head = init_git_repo(tmp_path)
    subprocess.run(["git", "checkout", "-b", "task/1368-fake"], cwd=tmp_path, check=True, capture_output=True, text=True)
    env = fake_env(tmp_path)
    env["FAKE_HEAD"] = head
    env["FAKE_REVIEW_VERDICT"] = "changes_requested"
    env["FAKE_REVIEW_FINDINGS"] = json.dumps(
        [
            {
                "category": "blocking_bug",
                "summary": "reviewer found a fake blocker",
                "notes": "details from reviewer artifact",
            }
        ]
    )
    den = RecordingDenClient()

    result = run_den_coder_reviewer_workflow(
        den_client=den,
        task_id=1368,
        prompt="Use the Den task context to implement and review task 1368.",
        run_root=tmp_path / ".den" / "runs",
        cwd=tmp_path,
        verify_git=True,
        coder={"run_id": "coder-run"},
        reviewer={"run_id": "reviewer-run"},
        env_overrides=env,
    )

    assert result.status == "completed"
    posted = [event for event in den.events if event[0] == "review_findings_posted"]
    assert posted == [
        (
            "review_findings_posted",
            1368,
            {"review_round_id": 321},
            "reviewer-run",
            "changes_requested",
            [
                {
                    "category": "blocking_bug",
                    "summary": "reviewer found a fake blocker",
                    "notes": "details from reviewer artifact",
                }
            ],
            "fake reviewer approved",
        )
    ]

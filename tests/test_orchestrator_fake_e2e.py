import subprocess

import pytest

from den_hermes.runtime_registry import RuntimeRegistryError, resolve_role_runtime
from den_hermes.orchestrator import (
    DenWorkflowAdapter,
    handle_review_outcome,
    run_tracked_coder_path,
    run_tracked_gate_role_path,
    run_tracked_reviewer_path,
)
from test_spawned_hermes_worker import FAKE_HEAD, fake_env, init_git_repo, read_fake_calls, write_runtime_registry


class StatefulFakeDenTools:
    def __init__(self, *, registration_failure_roles=None, completion_failure_roles=None):
        self.calls = []
        self.completions = {}
        self.worker_runs = {}
        self.review_round_id = 321
        self.message_id = 7000
        self.finding_id = 9000
        self.registration_failure_roles = registration_failure_roles or set()
        self.completion_failure_roles = completion_failure_roles or set()
        self.workflow_summary = {"current_review_state": {"review_round_id": self.review_round_id, "verdict": "changes_requested"}}

    def _message_id(self):
        self.message_id += 1
        return self.message_id

    def mcp_den_get_task_workflow_summary(self, **kwargs):
        self.calls.append(("get_task_workflow_summary", kwargs))
        return self.workflow_summary

    def mcp_den_prepare_coder_context_packet(self, **kwargs):
        self.calls.append(("prepare_coder_context_packet", kwargs))
        return {"message_id": self._message_id()}

    def mcp_den_prepare_reviewer_context_packet(self, **kwargs):
        self.calls.append(("prepare_reviewer_context_packet", kwargs))
        return {"message_id": self._message_id()}

    def mcp_den_prepare_validator_context_packet(self, **kwargs):
        self.calls.append(("prepare_validator_context_packet", kwargs))
        return {"message_id": self._message_id()}

    def mcp_den_prepare_drift_checker_context_packet(self, **kwargs):
        self.calls.append(("prepare_drift_checker_context_packet", kwargs))
        return {"message_id": self._message_id()}

    def mcp_den_prepare_packet_auditor_context_packet(self, **kwargs):
        self.calls.append(("prepare_packet_auditor_context_packet", kwargs))
        return {"message_id": self._message_id()}

    def mcp_den_register_worker_run(self, **kwargs):
        self.calls.append(("register_worker_run", kwargs))
        if kwargs["role"] in self.registration_failure_roles:
            return {"error": f"registration rejected for {kwargs['role']}"}
        self.worker_runs[kwargs["run_id"]] = {**kwargs, "state": "registered"}
        return {"worker_run": {"run_id": kwargs["run_id"], "state": "registered"}}

    def mcp_den_send_message(self, **kwargs):
        self.calls.append(("send_message", kwargs))
        return {"id": self._message_id()}

    def mcp_den_post_worker_completion_packet(self, **kwargs):
        self.calls.append(("post_worker_completion_packet", kwargs))
        if kwargs["role"] in self.completion_failure_roles:
            return {"completion_state": "rejected", "summary": f"completion rejected for {kwargs['role']}"}
        completion = {"completion_state": "completed", **kwargs}
        self.completions[kwargs["run_id"]] = completion
        if kwargs["run_id"] in self.worker_runs:
            self.worker_runs[kwargs["run_id"]]["state"] = kwargs["status"]
        return completion

    def mcp_den_get_latest_worker_completion(self, **kwargs):
        self.calls.append(("get_latest_worker_completion", kwargs))
        return self.completions.get(kwargs["run_id"], {"completion_state": "missing_packet"})

    def mcp_den_get_worker_run_status(self, **kwargs):
        self.calls.append(("get_worker_run_status", kwargs))
        return {"worker_run": {"run_id": kwargs["run_id"], "state": self.worker_runs[kwargs["run_id"]]["state"]}}

    def mcp_den_request_review(self, **kwargs):
        self.calls.append(("request_review", kwargs))
        return {"review_round_id": self.review_round_id, "message_id": self._message_id()}

    def mcp_den_create_review_finding(self, **kwargs):
        self.calls.append(("create_review_finding", kwargs))
        self.finding_id += 1
        return {"id": self.finding_id}

    def mcp_den_post_review_findings(self, **kwargs):
        self.calls.append(("post_review_findings", kwargs))
        return {"message_id": self._message_id()}

    def mcp_den_set_review_verdict(self, **kwargs):
        self.calls.append(("set_review_verdict", kwargs))
        self.workflow_summary = {"current_review_state": {"review_round_id": kwargs["review_round_id"], "verdict": kwargs["verdict"]}}
        return {"ok": True}

    def mcp_den_respond_to_review_finding(self, **kwargs):
        self.calls.append(("respond_to_review_finding", kwargs))
        return {"id": kwargs["review_finding_id"], "status": kwargs["status"]}


def make_adapter(tools):
    return DenWorkflowAdapter(tools=tools, project_id="den-hermes-bridge", requested_by="den-hermes-runner")


def coder_artifact_from_result(result):
    return {
        "run_id": result.run_id,
        "branch": result.branch,
        "head_commit": result.head_commit,
        "tests_run": [{"command": "pytest tests/ -q", "result": "passed"}],
        "summary": "fake coder completed",
    }


def test_fake_e2e_happy_path_coder_reviewer_gates_and_done_ready(tmp_path):
    head = init_git_repo(tmp_path)
    subprocess.run(["git", "checkout", "-b", "task/1400-fake-e2e"], cwd=tmp_path, check=True, capture_output=True, text=True)
    env = fake_env(tmp_path)
    env["FAKE_BRANCH"] = "task/1400-fake-e2e"
    env["FAKE_HEAD"] = head
    tools = StatefulFakeDenTools()
    adapter = make_adapter(tools)
    registry = write_runtime_registry(tmp_path)

    coder = run_tracked_coder_path(
        adapter,
        task_id=1400,
        prompt="Implement fake task.",
        run_id="coder-run",
        cwd=tmp_path,
        env_overrides=env,
        runtime_registry_path=registry,
        verify_git=True,
    )
    reviewer = run_tracked_reviewer_path(
        adapter,
        task_id=1400,
        prompt="Review fake task.",
        run_id="reviewer-run",
        coder_artifact=coder_artifact_from_result(coder),
        cwd=tmp_path,
        env_overrides=env,
        runtime_registry_path=registry,
    )
    validator = run_tracked_gate_role_path(
        adapter,
        task_id=1400,
        role="validator",
        prompt="Validate fake task.",
        run_id="validator-run",
        branch=coder.branch,
        head_commit=coder.head_commit,
        cwd=tmp_path,
        env_overrides=env,
        runtime_registry_path=registry,
    )
    drift = run_tracked_gate_role_path(
        adapter,
        task_id=1400,
        role="drift_checker",
        prompt="Check fake drift.",
        run_id="drift-run",
        branch=coder.branch,
        head_commit=coder.head_commit,
        cwd=tmp_path,
        env_overrides=env,
        runtime_registry_path=registry,
    )
    audit = run_tracked_gate_role_path(
        adapter,
        task_id=1400,
        role="packet_auditor",
        prompt="Audit fake packets.",
        run_id="audit-run",
        branch=coder.branch,
        head_commit=coder.head_commit,
        cwd=tmp_path,
        env_overrides=env,
        runtime_registry_path=registry,
    )
    done = handle_review_outcome(
        adapter,
        task_id=1400,
        review_state={"verdict": reviewer.verdict, "attempt": 1, "findings": []},
        prompt="No retry expected.",
        next_coder_run_id="unused",
        coder_runner=lambda *_args, **_kwargs: pytest.fail("done-ready path should not retry coder"),
    )

    assert coder.status == reviewer.status == validator.status == drift.status == audit.status == "completed"
    assert done.status == "done_ready"
    assert [tools.completions[run_id]["packet_type"] for run_id in ["coder-run", "reviewer-run", "validator-run", "drift-run", "audit-run"]] == [
        "implementation_packet",
        "review_findings_packet",
        "validation_packet",
        "drift_check_packet",
        "packet_audit_packet",
    ]
    assert [call["env"]["DEN_WORKER_ROLE"] for call in read_fake_calls(tmp_path)] == [
        "coder",
        "reviewer",
        "validator",
        "drift_checker",
        "packet_auditor",
    ]


@pytest.mark.parametrize("role", ["coder", "reviewer", "validator", "drift_checker", "packet_auditor"])
def test_fake_e2e_registration_failure_prevents_role_launch(tmp_path, role):
    head = init_git_repo(tmp_path)
    subprocess.run(["git", "checkout", "-b", "task/1400-fake-e2e"], cwd=tmp_path, check=True, capture_output=True, text=True)
    env = fake_env(tmp_path)
    env["FAKE_BRANCH"] = "task/1400-fake-e2e"
    env["FAKE_HEAD"] = head
    tools = StatefulFakeDenTools(registration_failure_roles={role})
    adapter = make_adapter(tools)
    registry = write_runtime_registry(tmp_path)

    if role == "coder":
        result = run_tracked_coder_path(adapter, task_id=1400, prompt="Implement.", run_id="coder-run", cwd=tmp_path, env_overrides=env, runtime_registry_path=registry)
    elif role == "reviewer":
        result = run_tracked_reviewer_path(
            adapter,
            task_id=1400,
            prompt="Review.",
            run_id="reviewer-run",
            coder_artifact={
                "run_id": "coder-run",
                "branch": "task/1400-fake-e2e",
                "head_commit": head,
                "tests_run": [{"command": "pytest -q", "result": "passed"}],
            },
            cwd=tmp_path,
            env_overrides=env,
            runtime_registry_path=registry,
        )
    else:
        result = run_tracked_gate_role_path(
            adapter,
            task_id=1400,
            role=role,
            prompt=f"Run {role} gate.",
            run_id=f"{role}-run",
            branch="task/1400-fake-e2e",
            head_commit=head,
            cwd=tmp_path,
            env_overrides=env,
            runtime_registry_path=registry,
        )

    assert result.status == "failed"
    assert "registration rejected" in result.error
    assert not any(call["env"]["DEN_WORKER_ROLE"] == role for call in read_fake_calls(tmp_path)) if (tmp_path / "fake-hermes-call.jsonl").exists() else True


def test_fake_e2e_completion_rejection_blocks_next_phase(tmp_path):
    head = init_git_repo(tmp_path)
    subprocess.run(["git", "checkout", "-b", "task/1400-fake-e2e"], cwd=tmp_path, check=True, capture_output=True, text=True)
    env = fake_env(tmp_path)
    env["FAKE_BRANCH"] = "task/1400-fake-e2e"
    env["FAKE_HEAD"] = head
    tools = StatefulFakeDenTools(completion_failure_roles={"coder"})
    adapter = make_adapter(tools)

    result = run_tracked_coder_path(
        adapter,
        task_id=1400,
        prompt="Implement.",
        run_id="coder-run",
        cwd=tmp_path,
        env_overrides=env,
        runtime_registry_path=write_runtime_registry(tmp_path),
        verify_git=True,
    )

    assert result.status == "failed"
    assert "completion rejected" in result.error
    assert "reviewer" not in [call["env"]["DEN_WORKER_ROLE"] for call in read_fake_calls(tmp_path)]


@pytest.mark.parametrize("role", ["validator", "drift_checker", "packet_auditor"])
def test_fake_e2e_gate_completion_rejection_is_fail_closed(tmp_path, role):
    env = fake_env(tmp_path)
    tools = StatefulFakeDenTools(completion_failure_roles={role})
    adapter = make_adapter(tools)

    result = run_tracked_gate_role_path(
        adapter,
        task_id=1400,
        role=role,
        prompt=f"Run {role} gate.",
        run_id=f"{role}-run",
        branch="task/1400-fake-e2e",
        head_commit=FAKE_HEAD,
        cwd=tmp_path,
        env_overrides=env,
        runtime_registry_path=write_runtime_registry(tmp_path),
    )

    assert result.status == "failed"
    assert "completion rejected" in result.error
    assert tools.completions == {}


def test_fake_e2e_reviewer_failure_posts_failure_packet(tmp_path):
    tools = StatefulFakeDenTools()
    adapter = make_adapter(tools)
    env = fake_env(tmp_path)
    env["FAKE_HERMES_REVIEWER_MODE"] = "identity_mismatch"

    result = run_tracked_reviewer_path(
        adapter,
        task_id=1400,
        prompt="Review.",
        run_id="reviewer-run",
        coder_artifact={
            "run_id": "coder-run",
            "branch": "task/branch",
            "head_commit": FAKE_HEAD,
            "tests_run": [{"command": "pytest -q", "result": "passed"}],
        },
        cwd=tmp_path,
        env_overrides=env,
        runtime_registry_path=write_runtime_registry(tmp_path),
    )

    assert result.status == "failed"
    assert tools.completions["reviewer-run"]["packet_type"] == "worker_failure_packet"




def test_fake_e2e_reviewer_skips_informational_findings(tmp_path):
    tools = StatefulFakeDenTools()
    adapter = make_adapter(tools)
    env = fake_env(tmp_path)
    env["FAKE_REVIEW_FINDINGS"] = '[{"category":"style","summary":"informational note"}]'

    result = run_tracked_reviewer_path(
        adapter,
        task_id=1400,
        prompt="Review.",
        run_id="reviewer-run",
        coder_artifact={
            "run_id": "coder-run",
            "branch": "task/branch",
            "head_commit": FAKE_HEAD,
            "tests_run": [{"command": "pytest -q", "result": "passed"}],
        },
        cwd=tmp_path,
        env_overrides=env,
        runtime_registry_path=write_runtime_registry(tmp_path),
    )

    assert result.status == "completed"
    assert result.verdict == "looks_good"
    assert result.finding_ids == []
    assert not [call for call in tools.calls if call[0] == "create_review_finding"]
    assert [call for call in tools.calls if call[0] == "post_review_findings"]


def test_fake_e2e_max_attempts_blocks_without_retry():
    tools = StatefulFakeDenTools()
    adapter = make_adapter(tools)

    result = handle_review_outcome(
        adapter,
        task_id=1400,
        review_state={"verdict": "changes_requested", "attempt": 3, "findings": [{"id": 9001, "category": "blocking_bug", "summary": "still broken"}]},
        prompt="Retry.",
        next_coder_run_id="unused",
        max_attempts=3,
        coder_runner=lambda *_args, **_kwargs: pytest.fail("max attempts must not launch coder"),
    )

    assert result.status == "blocked"
    assert "max attempts" in result.reason


def test_fake_e2e_hidden_runtime_override_is_rejected(tmp_path):
    registry = write_runtime_registry(tmp_path)

    with pytest.raises(RuntimeRegistryError, match="allow_runtime_override"):
        resolve_role_runtime(
            "coder",
            registry_path=registry,
            run_id="coder-run",
            overrides={"provider": "hidden-provider"},
            allow_runtime_override=False,
        )

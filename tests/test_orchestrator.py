import json
import subprocess

import pytest

from den_hermes.orchestrator import (
    DenWorkflowAdapter,
    GateRolePathResult,
    OrchestratorAction,
    OrchestratorActionType,
    OrchestratorStopResult,
    decide_next_action,
    main,
    run_tracked_gate_role_path,
    run_tracked_coder_path,
    run_tracked_reviewer_path,
    handle_review_outcome,
    CoderPathResult,
    lease_aware_stop,
    PROJECT_ORCHESTRATOR_DIAGNOSTIC_GUARDRAILS,
    _artifact_with_repo_metadata,
    _finalize_pool_assignment,
    _verify_promotion_head_match,
    _coder_prompt_with_packet,
    _reviewer_prompt_with_packet,
    _gate_prompt_with_packet,
    WakeStateProjection,
    project_wake_state,
    enrich_final_status,
)
from den_hermes.worker_launcher import HermesWorkerResult
from test_spawned_hermes_worker import FAKE_HEAD, fake_env, init_git_repo, read_fake_calls, write_runtime_registry


class RecordingWorkflowTools:
    def __init__(self, *, summary, next_action):
        self.summary = summary
        self.next_action = next_action
        self.calls = []

    def mcp_den_get_task_workflow_summary(self, **kwargs):
        self.calls.append(("get_task_workflow_summary", kwargs))
        return self.summary

    def mcp_den_determine_orchestrator_next_action(self, **kwargs):
        self.calls.append(("determine_orchestrator_next_action", kwargs))
        return self.next_action

    def mcp_den_get_latest_worker_completion(self, **kwargs):
        self.calls.append(("get_latest_worker_completion", kwargs))
        return {"completion_state": "missing_packet"}


def make_adapter(tools):
    return DenWorkflowAdapter(
        tools=tools,
        project_id="den-hermes-bridge",
        requested_by="den-hermes-runner",
    )


def test_terminal_done_task_is_noop_and_does_not_launch_workers():
    tools = RecordingWorkflowTools(
        summary={"task": {"id": 1395, "status": "done"}},
        next_action={"next_action": "done", "reason": "task already complete"},
    )

    action = decide_next_action(make_adapter(tools), task_id=1395)

    assert action == OrchestratorAction(
        type=OrchestratorActionType.DONE,
        reason="task already complete",
        role=None,
        details={"task_status": "done"},
    )
    assert [name for name, _ in tools.calls] == [
        "get_task_workflow_summary",
        "determine_orchestrator_next_action",
    ]


def test_coder_needed_state_maps_to_start_coder_action():
    tools = RecordingWorkflowTools(
        summary={"task": {"id": 1395, "status": "in_progress"}, "latest_packets": []},
        next_action={"next_action": "start_coder", "reason": "no coder completion packet exists"},
    )

    action = decide_next_action(make_adapter(tools), task_id=1395)

    assert action.type is OrchestratorActionType.START_CODER
    assert action.role == "coder"
    assert action.reason == "no coder completion packet exists"
    assert action.details["task_status"] == "in_progress"


def test_nested_den_decision_launch_coder_maps_to_start_coder_action():
    tools = RecordingWorkflowTools(
        summary={"task": {"id": 1415, "status": "in_progress"}},
        next_action={"decision": {"next_action": "launch_coder", "reason": "live Den decision"}},
    )

    action = decide_next_action(make_adapter(tools), task_id=1415)

    assert action.type is OrchestratorActionType.START_CODER
    assert action.role == "coder"
    assert action.reason == "live Den decision"


@pytest.mark.parametrize(
    ("den_action", "expected_type", "expected_role"),
    [
        ("launch_validator", OrchestratorActionType.START_VALIDATOR, "validator"),
        ("launch_drift_checker", OrchestratorActionType.START_DRIFT_CHECKER, "drift_checker"),
        ("launch_packet_auditor", OrchestratorActionType.START_PACKET_AUDITOR, "packet_auditor"),
    ],
)
def test_gate_launch_actions_map_to_gate_roles(den_action, expected_type, expected_role):
    tools = RecordingWorkflowTools(
        summary={"task": {"id": 1415, "status": "review"}},
        next_action={"next_action": den_action, "reason": f"run {expected_role}"},
    )

    action = decide_next_action(make_adapter(tools), task_id=1415)

    assert action.type is expected_type
    assert action.role == expected_role
    assert action.reason == f"run {expected_role}"


def test_completed_coder_and_pending_review_maps_to_start_reviewer_action():
    tools = RecordingWorkflowTools(
        summary={
            "task": {"id": 1395, "status": "review"},
            "latest_worker_completions": {"coder": {"completion_state": "completed", "run_id": "coder-run"}},
            "review_rounds": [{"id": 321, "verdict": None}],
        },
        next_action={
            "next_action": "start_reviewer",
            "reason": "coder completed and review round 321 has no verdict",
            "review_round_id": 321,
        },
    )

    action = decide_next_action(make_adapter(tools), task_id=1395)

    assert action.type is OrchestratorActionType.START_REVIEWER
    assert action.role == "reviewer"
    assert action.details["review_round_id"] == 321
    assert "review round 321" in action.reason


def test_changes_requested_maps_to_retry_handler_action():
    tools = RecordingWorkflowTools(
        summary={
            "task": {"id": 1395, "status": "review"},
            "current_review_state": {"verdict": "changes_requested"},
            "unresolved_findings": [{"id": 9001, "category": "blocking_bug"}],
        },
        next_action={
            "next_action": "handle_changes_requested",
            "reason": "reviewer requested changes for unresolved finding 9001",
            "finding_ids": [9001],
        },
    )

    action = decide_next_action(make_adapter(tools), task_id=1395)

    assert action.type is OrchestratorActionType.HANDLE_CHANGES_REQUESTED
    assert action.role == "coder"
    assert action.details["finding_ids"] == [9001]


def test_blocked_or_needs_input_state_maps_to_blocked_action():
    tools = RecordingWorkflowTools(
        summary={"task": {"id": 1395, "status": "blocked"}},
        next_action={"next_action": "needs_input", "reason": "dependency #1394 requires planner decision"},
    )

    action = decide_next_action(make_adapter(tools), task_id=1395)

    assert action.type is OrchestratorActionType.BLOCKED
    assert action.role is None
    assert "planner decision" in action.reason


def test_adapter_wraps_mcp_tools_for_workflow_summary_and_den_decision():
    tools = RecordingWorkflowTools(
        summary=json.dumps({"task": {"id": 1400, "status": "planned"}}),
        next_action={"result": json.dumps({"next_action": "await_coder", "reason": "coder run still active"})},
    )
    adapter = make_adapter(tools)

    summary = adapter.get_task_workflow_summary(task_id=1400)
    decision = adapter.determine_orchestrator_next_action(task_id=1400, max_attempts=5)

    assert summary["task"]["status"] == "planned"
    assert decision["next_action"] == "await_coder"
    assert tools.calls == [
        ("get_task_workflow_summary", {"task_id": 1400}),
        (
            "determine_orchestrator_next_action",
            {"project_id": "den-hermes-bridge", "task_id": 1400, "max_attempts": 5},
        ),
    ]



class RecordingCoderTools:
    def __init__(
        self,
        *,
        launch_log=None,
        registration_response=None,
        completion_response=None,
        post_findings_response=None,
        verdict_response=None,
    ):
        self.calls = []
        self.launch_log = launch_log
        self.registration_response = registration_response or {"worker_run": {"run_id": "coder-run"}}
        self.completion_response = completion_response or {"completion_state": "completed", "run_id": "coder-run"}
        self.post_findings_response = post_findings_response or {"message_id": 655}
        self.verdict_response = verdict_response or {"ok": True}
        self.completions = {}
        self.workflow_summary = {"current_review_state": {"review_round_id": 321, "verdict": "changes_requested"}}

    def mcp_den_get_task_workflow_summary(self, **kwargs):
        self.calls.append(("get_task_workflow_summary", kwargs))
        return self.workflow_summary

    def mcp_den_prepare_coder_context_packet(self, **kwargs):
        self.calls.append(("prepare_coder_context_packet", kwargs))
        return {"message_id": 5791}

    def mcp_den_prepare_reviewer_context_packet(self, **kwargs):
        self.calls.append(("prepare_reviewer_context_packet", kwargs))
        return {"message_id": 5792}

    def mcp_den_prepare_validator_context_packet(self, **kwargs):
        self.calls.append(("prepare_validator_context_packet", kwargs))
        return {"message_id": 5793}

    def mcp_den_prepare_drift_checker_context_packet(self, **kwargs):
        self.calls.append(("prepare_drift_checker_context_packet", kwargs))
        return {"message_id": 5794}

    def mcp_den_prepare_packet_auditor_context_packet(self, **kwargs):
        self.calls.append(("prepare_packet_auditor_context_packet", kwargs))
        return {"message_id": 5795}

    def mcp_den_request_review(self, **kwargs):
        self.calls.append(("request_review", kwargs))
        return {"review_round_id": 321, "message_id": 654}

    def mcp_den_register_worker_run(self, **kwargs):
        if self.launch_log is not None:
            assert not self.launch_log.exists(), "worker launched before Den registration"
        self.calls.append(("register_worker_run", kwargs))
        if self.registration_response.get("error"):
            raise RuntimeError(self.registration_response["error"])
        return self.registration_response

    def mcp_den_send_message(self, **kwargs):
        self.calls.append(("send_message", kwargs))
        return {"id": 1001}

    def mcp_den_post_worker_completion_packet(self, **kwargs):
        self.calls.append(("post_worker_completion_packet", kwargs))
        if self.completion_response.get("completion_state") == "completed":
            self.completions[kwargs["run_id"]] = {**self.completion_response, **kwargs}
        return self.completion_response

    def mcp_den_get_latest_worker_completion(self, **kwargs):
        self.calls.append(("get_latest_worker_completion", kwargs))
        return self.completions.get(kwargs["run_id"], {"completion_state": "missing_packet"})

    def mcp_den_get_worker_run_status(self, **kwargs):
        self.calls.append(("get_worker_run_status", kwargs))
        return {"worker_run": {"run_id": kwargs["run_id"], "state": "completed"}}

    def mcp_den_create_review_finding(self, **kwargs):
        finding_id = 9000 + len([call for call in self.calls if call[0] == "create_review_finding"])
        self.calls.append(("create_review_finding", kwargs))
        return {"id": finding_id}

    def mcp_den_post_review_findings(self, **kwargs):
        self.calls.append(("post_review_findings", kwargs))
        return self.post_findings_response

    def mcp_den_set_review_verdict(self, **kwargs):
        self.calls.append(("set_review_verdict", kwargs))
        return self.verdict_response

    def mcp_den_respond_to_review_finding(self, **kwargs):
        self.calls.append(("respond_to_review_finding", kwargs))
        return {"id": kwargs["review_finding_id"], "status": kwargs.get("status")}

    def mcp_den_set_review_finding_status(self, **kwargs):
        self.calls.append(("set_review_finding_status", kwargs))
        return {"id": kwargs["review_finding_id"], "status": kwargs.get("status")}

    def mcp_den_append_checkpoint(self, **kwargs):
        self.calls.append(("append_checkpoint", kwargs))
        return {"checkpoint_id": 5001}

    def mcp_den_record_cleanup_evidence(self, **kwargs):
        self.calls.append(("record_cleanup_evidence", kwargs))
        return {"ok": True}

    def mcp_den_release_assignment(self, **kwargs):
        self.calls.append(("release_assignment", kwargs))
        return {"ok": True}

    def mcp_den_send_user_notification(self, **kwargs):
        self.calls.append(("send_user_notification", kwargs))
        return {"id": 9003}

    def mcp_den_list_active_leases(self, **kwargs):
        self.calls.append(("list_active_leases", kwargs))
        return {"leases": [], "active_leases": []}

    def mcp_den_get_worker_pool_summary(self, **kwargs):
        self.calls.append(("get_worker_pool_summary", kwargs))
        return {"active_assignments": [], "assignments": []}

    def mcp_den_release_orchestrator_lease(self, **kwargs):
        self.calls.append(("release_orchestrator_lease", kwargs))
        return {"ok": True}

    def mcp_den_fail_assignment(self, **kwargs):
        self.calls.append(("fail_assignment", kwargs))
        return {"ok": True}


def test_artifact_repo_metadata_uses_orchestrator_values_over_worker_claims():
    artifact = {
        "branch": "worker/wrong",
        "head_commit": "0" * 40,
        "base_commit": "2" * 40,
        "review_round_id": 999,
    }

    enriched = _artifact_with_repo_metadata(
        artifact,
        branch="task/1415-correct",
        head_commit="1" * 40,
        base_commit="3" * 40,
        review_round_id=321,
    )

    assert enriched["branch"] == "task/1415-correct"
    assert enriched["head_commit"] == "1" * 40
    assert enriched["base_commit"] == "3" * 40
    assert enriched["review_round_id"] == 321


def test_cli_prints_json_action_without_launching_workers(monkeypatch, capsys):
    def fake_build_adapter(*, project_id, requested_by):
        assert project_id == "den-hermes-bridge"
        assert requested_by == "den-hermes-runner"
        return make_adapter(
            RecordingWorkflowTools(
                summary={"task": {"id": 1395, "status": "planned"}},
                next_action={"next_action": "start_coder", "reason": "ready"},
            )
        )

    monkeypatch.setattr("den_hermes.orchestrator.build_mcp_adapter", fake_build_adapter)

    exit_code = main(["--project-id", "den-hermes-bridge", "--task-id", "1395", "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["type"] == "start_coder"
    assert payload["role"] == "coder"
    assert payload["reason"] == "ready"


def test_tracked_coder_path_prepares_context_registers_before_launch_and_posts_completion(tmp_path):
    head = init_git_repo(tmp_path)
    subprocess.run(["git", "checkout", "-b", "task/1368-fake"], cwd=tmp_path, check=True, capture_output=True, text=True)
    env = fake_env(tmp_path)
    env["FAKE_HEAD"] = head
    tools = RecordingCoderTools(launch_log=tmp_path / "fake-hermes-call.jsonl")
    adapter = make_adapter(tools)

    result = run_tracked_coder_path(
        adapter,
        task_id=1396,
        prompt="Implement the task from the Den coder context packet.",
        run_id="coder-run",
        cwd=tmp_path,
        env_overrides=env,
        runtime_registry_path=write_runtime_registry(tmp_path),
        verify_git=True,
    )

    assert result.status == "completed"
    assert result.run_id == "coder-run"
    assert result.branch == "task/1368-fake"
    assert result.head_commit == head
    assert result.latest_completion["completion_state"] == "completed"
    assert [name for name, _ in tools.calls] == [
        "prepare_coder_context_packet",
        "register_worker_run",
        "send_message",
        "post_worker_completion_packet",
        "get_latest_worker_completion",
        "get_worker_run_status",
    ]
    registration = tools.calls[1][1]
    assert registration["prompt_packet_message_id"] == 5791
    assert registration["profile"] == "den-coder-profile"
    assert registration["provider"] == "provider-coder"
    assert registration["model"] == "model-coder"
    assert registration["toolsets"] == "terminal,file"
    assert "runtime_id" not in registration
    assert registration["artifact_path"] == result.artifact_path
    assert read_fake_calls(tmp_path)[0]["env"]["DEN_RUN_ID"] == "coder-run"


def test_tracked_coder_path_propagates_claimed_review_findings_from_artifact(tmp_path):
    head = init_git_repo(tmp_path)
    subprocess.run(["git", "checkout", "-b", "task/1368-fake"], cwd=tmp_path, check=True, capture_output=True, text=True)
    env = fake_env(tmp_path)
    env["FAKE_HEAD"] = head
    env["FAKE_CLAIMED_FINDING_IDS"] = json.dumps([9001])
    env["FAKE_RESPONSE_NOTES"] = "implemented requested fix"

    result = run_tracked_coder_path(
        make_adapter(RecordingCoderTools()),
        task_id=1396,
        prompt="Implement retry.",
        run_id="coder-run",
        cwd=tmp_path,
        env_overrides=env,
        runtime_registry_path=write_runtime_registry(tmp_path),
        verify_git=True,
    )

    assert result.status == "completed"
    assert result.claimed_finding_ids == [9001]
    assert result.response_notes == "implemented requested fix"


def test_tracked_coder_path_registration_failure_prevents_launch(tmp_path):
    tools = RecordingCoderTools(registration_response={"error": "registration rejected"})
    adapter = make_adapter(tools)

    result = run_tracked_coder_path(
        adapter,
        task_id=1396,
        prompt="Implement.",
        run_id="coder-run",
        cwd=tmp_path,
        env_overrides=fake_env(tmp_path),
        runtime_registry_path=write_runtime_registry(tmp_path),
    )

    assert result.status == "failed"
    assert "registration rejected" in result.error
    assert [name for name, _ in tools.calls] == ["prepare_coder_context_packet", "register_worker_run"]
    assert not (tmp_path / "fake-hermes-call.jsonl").exists()


def test_tracked_coder_path_missing_artifact_posts_failure_and_skips_completion(tmp_path):
    tools = RecordingCoderTools()
    adapter = make_adapter(tools)

    result = run_tracked_coder_path(
        adapter,
        task_id=1396,
        prompt="Implement.",
        run_id="coder-run",
        cwd=tmp_path,
        env_overrides=fake_env(tmp_path, mode="missing_artifact"),
        runtime_registry_path=write_runtime_registry(tmp_path),
    )

    assert result.status == "failed"
    assert "Missing completion artifact" in result.error
    call_names = [name for name, _ in tools.calls]
    assert call_names == [
        "prepare_coder_context_packet",
        "register_worker_run",
        "send_message",
        "post_worker_completion_packet",
        "post_worker_completion_packet",  # budget-exhaustion backstop
    ]
    failure_packet = tools.calls[-1][1]
    assert failure_packet["packet_type"] == "worker_failure_packet"
    assert failure_packet["status"] == "failed"
    assert "budget exhausted" in failure_packet["summary"].lower()


def test_tracked_coder_path_git_mismatch_posts_failure_before_completion(tmp_path):
    init_git_repo(tmp_path)
    tools = RecordingCoderTools()
    adapter = make_adapter(tools)

    result = run_tracked_coder_path(
        adapter,
        task_id=1396,
        prompt="Implement.",
        run_id="coder-run",
        cwd=tmp_path,
        env_overrides=fake_env(tmp_path),
        runtime_registry_path=write_runtime_registry(tmp_path),
        verify_git=True,
    )

    assert result.status == "failed"
    assert "branch" in result.error.lower()
    assert [name for name, _ in tools.calls] == [
        "prepare_coder_context_packet",
        "register_worker_run",
        "send_message",
        "post_worker_completion_packet",
    ]
    assert tools.calls[-1][1]["packet_type"] == "worker_failure_packet"


def coder_artifact(branch="task/1368-fake", head=FAKE_HEAD):
    return {
        "status": "completed",
        "branch": branch,
        "head_commit": head,
        "tests_run": [{"command": "pytest tests/ -q", "result": "passed"}],
        "summary": "fake coder completed",
    }


def test_tracked_reviewer_path_requires_coder_artifact_before_launch(tmp_path):
    tools = RecordingCoderTools()
    adapter = make_adapter(tools)

    result = run_tracked_reviewer_path(
        adapter,
        task_id=1397,
        prompt="Review.",
        run_id="reviewer-run",
        coder_artifact={"branch": "task/missing-head"},
        cwd=tmp_path,
        env_overrides=fake_env(tmp_path),
        runtime_registry_path=write_runtime_registry(tmp_path),
    )

    assert result.status == "failed"
    assert "head_commit" in result.error
    assert tools.calls == []
    assert not (tmp_path / "fake-hermes-call.jsonl").exists()


def test_tracked_reviewer_path_registration_failure_prevents_launch(tmp_path):
    tools = RecordingCoderTools(registration_response={"error": "reviewer registration rejected"})
    adapter = make_adapter(tools)

    result = run_tracked_reviewer_path(
        adapter,
        task_id=1397,
        prompt="Review.",
        run_id="reviewer-run",
        coder_artifact=coder_artifact(),
        cwd=tmp_path,
        env_overrides=fake_env(tmp_path),
        runtime_registry_path=write_runtime_registry(tmp_path),
    )

    assert result.status == "failed"
    assert "reviewer registration rejected" in result.error
    assert [name for name, _ in tools.calls] == [
        "request_review",
        "prepare_reviewer_context_packet",
        "register_worker_run",
    ]
    assert not (tmp_path / "fake-hermes-call.jsonl").exists()


def test_tracked_reviewer_changes_requested_creates_findings_and_verdict(tmp_path):
    env = fake_env(tmp_path)
    env["FAKE_REVIEW_VERDICT"] = "changes_requested"
    env["FAKE_REVIEW_FINDINGS"] = json.dumps([
        {"category": "blocking_bug", "summary": "fix bug", "notes": "details"}
    ])
    env["FAKE_REVIEW_TESTS_RUN"] = json.dumps([{"command": "python -m pytest -q", "result": "67 passed"}])
    tools = RecordingCoderTools()
    adapter = make_adapter(tools)

    result = run_tracked_reviewer_path(
        adapter,
        task_id=1397,
        prompt="Review.",
        run_id="reviewer-run",
        coder_artifact=coder_artifact(),
        cwd=tmp_path,
        env_overrides=env,
        runtime_registry_path=write_runtime_registry(tmp_path),
    )

    assert result.status == "completed"
    assert result.verdict == "changes_requested"
    assert result.finding_ids == [9000]
    assert [name for name, _ in tools.calls] == [
        "request_review",
        "prepare_reviewer_context_packet",
        "register_worker_run",
        "send_message",
        "create_review_finding",
        "post_worker_completion_packet",
        "post_review_findings",
        "set_review_verdict",
        "get_latest_worker_completion",
        "get_worker_run_status",
    ]
    completion = tools.calls[5][1]
    assert completion["packet_type"] == "review_findings_packet"
    assert json.loads(completion["tests_run"]) == [{"command": "python -m pytest -q", "result": "67 passed"}]
    assert json.loads(completion["finding_ids"]) == [9000]
    finding = tools.calls[4][1]
    assert finding["category"] == "blocking_bug"
    assert finding["summary"] == "fix bug"
    assert tools.calls[7][1]["verdict"] == "changes_requested"


def test_tracked_reviewer_completion_includes_review_and_repo_metadata(tmp_path):
    tools = RecordingCoderTools()
    adapter = make_adapter(tools)

    result = run_tracked_reviewer_path(
        adapter,
        task_id=1397,
        prompt="Review.",
        run_id="reviewer-run",
        coder_artifact=coder_artifact(),
        cwd=tmp_path,
        env_overrides=fake_env(tmp_path),
        runtime_registry_path=write_runtime_registry(tmp_path),
        base_branch="main",
        base_commit="1" * 40,
    )

    assert result.status == "completed"
    completion = [kwargs for name, kwargs in tools.calls if name == "post_worker_completion_packet"][0]
    assert completion["branch"] == "task/1368-fake"
    assert completion["head_commit"] == FAKE_HEAD
    assert completion["base_commit"] == "1" * 40
    assert completion["review_round_id"] == 321


def test_tracked_reviewer_updates_existing_findings_instead_of_creating_new_ones(tmp_path):
    env = fake_env(tmp_path)
    env["FAKE_REVIEW_FINDINGS"] = json.dumps(
        [{"id": 740, "status": "verified_fixed", "notes": "confirmed fixed by diff"}]
    )
    tools = RecordingCoderTools()
    adapter = make_adapter(tools)

    result = run_tracked_reviewer_path(
        adapter,
        task_id=1397,
        prompt="Review.",
        run_id="reviewer-run",
        coder_artifact=coder_artifact(),
        cwd=tmp_path,
        env_overrides=env,
        runtime_registry_path=write_runtime_registry(tmp_path),
    )

    assert result.status == "completed"
    assert result.finding_ids == [740]
    call_names = [name for name, _ in tools.calls]
    assert "create_review_finding" not in call_names
    assert "set_review_finding_status" in call_names
    status_call = [kwargs for name, kwargs in tools.calls if name == "set_review_finding_status"][0]
    assert status_call["review_finding_id"] == 740
    assert status_call["status"] == "verified_fixed"
    completion = [kwargs for name, kwargs in tools.calls if name == "post_worker_completion_packet"][0]
    assert json.loads(completion["finding_ids"]) == [740]


def test_tracked_reviewer_looks_good_sets_verdict_without_findings(tmp_path):
    tools = RecordingCoderTools()
    adapter = make_adapter(tools)

    result = run_tracked_reviewer_path(
        adapter,
        task_id=1397,
        prompt="Review.",
        run_id="reviewer-run",
        coder_artifact=coder_artifact(),
        cwd=tmp_path,
        env_overrides=fake_env(tmp_path),
        runtime_registry_path=write_runtime_registry(tmp_path),
    )

    assert result.status == "completed"
    assert result.verdict == "looks_good"
    assert result.finding_ids == []
    assert "create_review_finding" not in [name for name, _ in tools.calls]
    assert [call for call in tools.calls if call[0] == "set_review_verdict"][0][1]["verdict"] == "looks_good"


def test_tracked_reviewer_path_fails_closed_when_verdict_publication_is_rejected(tmp_path):
    tools = RecordingCoderTools(verdict_response={"status": "error", "summary": "verdict rejected"})
    adapter = make_adapter(tools)

    result = run_tracked_reviewer_path(
        adapter,
        task_id=1397,
        prompt="Review.",
        run_id="reviewer-run",
        coder_artifact=coder_artifact(),
        cwd=tmp_path,
        env_overrides=fake_env(tmp_path),
        runtime_registry_path=write_runtime_registry(tmp_path),
    )

    assert result.status == "failed"
    assert "verdict rejected" in result.error
    assert [name for name, _ in tools.calls][-2:] == ["post_review_findings", "set_review_verdict"]


def test_review_outcome_changes_requested_retries_coder_and_claims_findings_fixed(tmp_path):
    tools = RecordingCoderTools()
    adapter = make_adapter(tools)
    launches = []

    def fake_coder_runner(adapter_arg, **kwargs):
        launches.append(kwargs)
        assert adapter_arg is adapter
        assert "fix bug" in kwargs["prompt"]
        return CoderPathResult(
            status="completed",
            run_id=kwargs["run_id"],
            branch="task/retry",
            head_commit="c" * 40,
            artifact_path="/tmp/retry/completion.json",
            claimed_finding_ids=[9001],
            response_notes="fixed the bug",
        )

    result = handle_review_outcome(
        adapter,
        task_id=1398,
        review_state={
            "verdict": "changes_requested",
            "attempt": 1,
            "review_round_id": 321,
            "findings": [{"id": 9001, "category": "blocking_bug", "summary": "fix bug"}],
        },
        prompt="Implement retry.",
        next_coder_run_id="coder-retry-2",
        max_attempts=3,
        coder_runner=fake_coder_runner,
    )

    assert result.status == "retry_launched"
    assert result.run_id == "coder-retry-2"
    assert result.finding_ids == [9001]
    assert launches[0]["run_id"] == "coder-retry-2"
    assert [name for name, _ in tools.calls] == ["get_task_workflow_summary", "respond_to_review_finding"]
    assert tools.calls[1][1]["status"] == "claimed_fixed"
    assert tools.calls[1][1]["response_notes"] == "fixed the bug"


def test_review_outcome_looks_good_returns_done_ready_without_launch():
    tools = RecordingCoderTools()
    adapter = make_adapter(tools)

    result = handle_review_outcome(
        adapter,
        task_id=1398,
        review_state={"verdict": "looks_good", "attempt": 1, "findings": []},
        prompt="No retry.",
        next_coder_run_id="unused",
        coder_runner=lambda *_args, **_kwargs: pytest.fail("should not launch coder"),
    )

    assert result.status == "done_ready"
    assert result.run_id is None
    assert tools.calls == []


def test_review_outcome_blocks_stale_changes_requested_when_newer_review_is_done():
    tools = RecordingCoderTools()
    tools.workflow_summary = {"current_review_state": {"review_round_id": 322, "verdict": "looks_good"}}
    adapter = make_adapter(tools)

    result = handle_review_outcome(
        adapter,
        task_id=1398,
        review_state={
            "verdict": "changes_requested",
            "attempt": 1,
            "review_round_id": 321,
            "findings": [{"id": 9001, "category": "blocking_bug", "summary": "old bug"}],
        },
        prompt="Retry.",
        next_coder_run_id="unused",
        max_attempts=3,
        coder_runner=lambda *_args, **_kwargs: pytest.fail("should not launch coder"),
    )

    assert result.status == "blocked"
    assert "newer review state" in result.reason
    assert [name for name, _ in tools.calls] == ["get_task_workflow_summary"]


def test_review_outcome_max_attempts_blocks_without_launch():
    tools = RecordingCoderTools()
    adapter = make_adapter(tools)

    result = handle_review_outcome(
        adapter,
        task_id=1398,
        review_state={
            "verdict": "changes_requested",
            "attempt": 3,
            "findings": [{"id": 9001, "category": "blocking_bug", "summary": "still broken"}],
        },
        prompt="Retry.",
        next_coder_run_id="unused",
        max_attempts=3,
        coder_runner=lambda *_args, **_kwargs: pytest.fail("should not launch coder"),
    )

    assert result.status == "blocked"
    assert "max attempts" in result.reason
    assert result.finding_ids == [9001]
    assert tools.calls == []


def test_review_outcome_follow_up_only_findings_are_deferred_without_launch():
    tools = RecordingCoderTools()
    adapter = make_adapter(tools)

    result = handle_review_outcome(
        adapter,
        task_id=1398,
        review_state={
            "verdict": "follow_up_needed",
            "attempt": 1,
            "findings": [{"id": 9002, "category": "follow_up_candidate", "summary": "nice to have"}],
        },
        prompt="Retry.",
        next_coder_run_id="unused",
        coder_runner=lambda *_args, **_kwargs: pytest.fail("should not launch coder"),
    )

    assert result.status == "follow_up_deferred"
    assert result.finding_ids == [9002]
    assert tools.calls == []


@pytest.mark.parametrize(
    ("role", "prepare_call", "packet_type", "profile", "evidence_key"),
    [
        ("validator", "prepare_validator_context_packet", "validation_packet", "den-validator-profile", "tests_run"),
        ("drift_checker", "prepare_drift_checker_context_packet", "drift_check_packet", "den-drift-profile", "checked_refs"),
        ("packet_auditor", "prepare_packet_auditor_context_packet", "packet_audit_packet", "den-audit-profile", "audited_packets"),
    ],
)
def test_tracked_gate_role_path_uses_role_context_runtime_and_packet_type(
    tmp_path, role, prepare_call, packet_type, profile, evidence_key
):
    tools = RecordingCoderTools()
    adapter = make_adapter(tools)

    result = run_tracked_gate_role_path(
        adapter,
        task_id=1399,
        role=role,
        prompt=f"Run {role} gate.",
        run_id=f"{role}-run",
        branch="task/1399-gates",
        head_commit=FAKE_HEAD,
        cwd=tmp_path,
        env_overrides=fake_env(tmp_path),
        runtime_registry_path=write_runtime_registry(tmp_path),
    )

    assert isinstance(result, GateRolePathResult)
    assert result.status == "completed"
    assert result.verdict == "passed"
    assert evidence_key in result.evidence
    assert [name for name, _ in tools.calls] == [
        prepare_call,
        "register_worker_run",
        "send_message",
        "post_worker_completion_packet",
        "get_latest_worker_completion",
        "get_worker_run_status",
    ]
    registration = tools.calls[1][1]
    assert registration["role"] == role
    assert registration["profile"] == profile
    assert registration["prompt_packet_message_id"] in {5793, 5794, 5795}
    completion = tools.calls[3][1]
    assert completion["packet_type"] == packet_type
    assert completion["branch"] == "task/1399-gates"
    assert completion["head_commit"] == FAKE_HEAD
    if role == "validator":
        assert json.loads(completion["tests_run"]) == result.evidence["tests_run"]
    else:
        assert "tests_run" not in completion
        assert evidence_key not in completion["summary"]
        assert completion["summary"] == f"Spawned-Hermes {role} gate completed with verdict passed."


def test_tracked_gate_role_path_does_not_post_worker_controlled_drift_summary(tmp_path):
    env = fake_env(tmp_path)
    env["FAKE_DRIFT_SUMMARY"] = "checked_refs include SECRET_TOKEN=abc123"
    tools = RecordingCoderTools()
    adapter = make_adapter(tools)

    result = run_tracked_gate_role_path(
        adapter,
        task_id=1399,
        role="drift_checker",
        prompt="Run drift gate.",
        run_id="drift-run",
        branch="task/1399-gates",
        head_commit=FAKE_HEAD,
        cwd=tmp_path,
        env_overrides=env,
        runtime_registry_path=write_runtime_registry(tmp_path),
    )

    assert result.status == "completed"
    completion = [kwargs for name, kwargs in tools.calls if name == "post_worker_completion_packet"][0]
    assert "SECRET_TOKEN" not in completion["summary"]
    assert "checked_refs" not in completion["summary"]
    assert completion["summary"] == "Spawned-Hermes drift_checker gate completed with verdict passed."


def test_tracked_gate_role_path_registration_failure_prevents_launch(tmp_path):
    tools = RecordingCoderTools(registration_response={"error": "validator registration rejected"})
    adapter = make_adapter(tools)

    result = run_tracked_gate_role_path(
        adapter,
        task_id=1399,
        role="validator",
        prompt="Validate.",
        run_id="validator-run",
        branch="task/1399-gates",
        head_commit=FAKE_HEAD,
        cwd=tmp_path,
        env_overrides=fake_env(tmp_path),
        runtime_registry_path=write_runtime_registry(tmp_path),
    )

    assert result.status == "failed"
    assert "validator registration rejected" in result.error
    assert [name for name, _ in tools.calls] == ["prepare_validator_context_packet", "register_worker_run"]
    assert not (tmp_path / "fake-hermes-call.jsonl").exists()


# ------------------------------------------------------------------
# Pool assignment lifecycle tests
# ------------------------------------------------------------------


def test_coder_path_with_assignment_finalizes_lifecycle(tmp_path):
    """Pool-managed coder path: assignment_id provided → full lifecycle."""
    head = init_git_repo(tmp_path)
    subprocess.run(["git", "checkout", "-b", "task/1368-fake"], cwd=tmp_path, check=True, capture_output=True, text=True)
    env = fake_env(tmp_path)
    env["FAKE_HEAD"] = head
    tools = RecordingCoderTools(launch_log=tmp_path / "fake-hermes-call.jsonl")
    adapter = make_adapter(tools)

    result = run_tracked_coder_path(
        adapter,
        task_id=1799,
        prompt="Implement pool completion release.",
        run_id="t1799-pool-coder",
        cwd=tmp_path,
        env_overrides=env,
        runtime_registry_path=write_runtime_registry(tmp_path),
        verify_git=True,
        assignment_id=11,
    )

    assert result.status == "completed"
    assert result.assignment_finalized is True

    # Verify call order: completion_packet → append_checkpoint → record_cleanup → release
    lifecycle_calls = [
        name for name, _ in tools.calls
        if name in ("post_worker_completion_packet", "append_checkpoint", "record_cleanup_evidence", "release_assignment")
    ]
    assert lifecycle_calls == [
        "post_worker_completion_packet",
        "append_checkpoint",
        "record_cleanup_evidence",
        "release_assignment",
    ]

    # Verify checkpoint has correct assignment context
    ckpt_call = [c for c in tools.calls if c[0] == "append_checkpoint"][0]
    assert ckpt_call[1]["assignment_id"] == 11
    assert ckpt_call[1]["run_id"] == "t1799-pool-coder"
    assert ckpt_call[1]["checkpoint_type"] == "completion"
    assert json.loads(ckpt_call[1]["payload"])["role"] == "coder"


def test_coder_path_without_assignment_skips_lifecycle(tmp_path):
    """Legacy coder path: no assignment_id → no lifecycle calls, assignment_finalized=False."""
    head = init_git_repo(tmp_path)
    subprocess.run(["git", "checkout", "-b", "task/1368-fake"], cwd=tmp_path, check=True, capture_output=True, text=True)
    env = fake_env(tmp_path)
    env["FAKE_HEAD"] = head
    tools = RecordingCoderTools()
    adapter = make_adapter(tools)

    result = run_tracked_coder_path(
        adapter,
        task_id=1396,
        prompt="Implement.",
        run_id="coder-run",
        cwd=tmp_path,
        env_overrides=env,
        runtime_registry_path=write_runtime_registry(tmp_path),
        verify_git=True,
    )

    assert result.status == "completed"
    assert result.assignment_finalized is False
    lifecycle_calls = [
        name for name, _ in tools.calls
        if name in ("append_checkpoint", "record_cleanup_evidence", "release_assignment")
    ]
    assert lifecycle_calls == []


def test_finalize_pool_assignment_requires_assignment_for_pool_managed_run():
    """Pool-managed finalization fails closed when assignment identity is missing."""
    tools = RecordingCoderTools()
    adapter = make_adapter(tools)

    with pytest.raises(RuntimeError, match="Missing assignment_id"):
        _finalize_pool_assignment(
            adapter,
            assignment_id=None,
            requires_assignment=True,
            run_id="pool-run",
            role="coder",
            success=True,
            summary="completed",
        )

    lifecycle_calls = [
        name for name, _ in tools.calls
        if name in ("append_checkpoint", "record_cleanup_evidence", "release_assignment")
    ]
    assert lifecycle_calls == []


def test_finalize_pool_assignment_allows_explicit_legacy_no_assignment_mode():
    """Non-pool legacy finalization may skip only when assignment is not required."""
    tools = RecordingCoderTools()
    adapter = make_adapter(tools)

    finalized = _finalize_pool_assignment(
        adapter,
        assignment_id=None,
        requires_assignment=False,
        run_id="legacy-run",
        role="coder",
        success=True,
        summary="completed",
    )

    assert finalized is False
    lifecycle_calls = [
        name for name, _ in tools.calls
        if name in ("append_checkpoint", "record_cleanup_evidence", "release_assignment")
    ]
    assert lifecycle_calls == []


def test_coder_failure_with_assignment_finalizes_lifecycle(tmp_path):
    """Pool-managed coder failure: assignment_id provided → failure checkpoint + cleanup + release."""
    tools = RecordingCoderTools()
    adapter = make_adapter(tools)

    result = run_tracked_coder_path(
        adapter,
        task_id=1799,
        prompt="Implement.",
        run_id="coder-run",
        cwd=tmp_path,
        env_overrides=fake_env(tmp_path, mode="missing_artifact"),
        runtime_registry_path=write_runtime_registry(tmp_path),
        assignment_id=11,
    )

    assert result.status == "failed"
    assert result.assignment_finalized is True

    lifecycle_calls = [
        name for name, _ in tools.calls
        if name in ("post_worker_completion_packet", "append_checkpoint", "record_cleanup_evidence", "release_assignment")
    ]
    assert lifecycle_calls == [
        "post_worker_completion_packet",  # failure packet
        "append_checkpoint",
        "record_cleanup_evidence",
        "release_assignment",
        "post_worker_completion_packet",  # budget-exhaustion backstop
    ]

    ckpt_call = [c for c in tools.calls if c[0] == "append_checkpoint"][0]
    assert ckpt_call[1]["checkpoint_type"] == "failure"
    assert ckpt_call[1]["assignment_id"] == 11


def test_reviewer_path_with_assignment_finalizes_lifecycle(tmp_path):
    """Pool-managed reviewer path: assignment_id provided → full lifecycle after completion."""
    tools = RecordingCoderTools()
    adapter = make_adapter(tools)

    result = run_tracked_reviewer_path(
        adapter,
        task_id=1799,
        prompt="Review changes.",
        run_id="reviewer-run",
        coder_artifact={
            "branch": "task/1799-pool",
            "head_commit": FAKE_HEAD,
            "tests_run": [{"command": "pytest", "result": "all passed"}],
        },
        cwd=tmp_path,
        env_overrides=fake_env(tmp_path, mode="reviewer_changes_requested"),
        runtime_registry_path=write_runtime_registry(tmp_path),
        assignment_id=12,
    )

    assert result.status == "completed"
    assert result.assignment_finalized is True

    lifecycle_calls = [
        name for name, _ in tools.calls
        if name in ("post_worker_completion_packet", "append_checkpoint", "record_cleanup_evidence", "release_assignment")
    ]
    assert lifecycle_calls == [
        "post_worker_completion_packet",
        "append_checkpoint",
        "record_cleanup_evidence",
        "release_assignment",
    ]

    ckpt_call = [c for c in tools.calls if c[0] == "append_checkpoint"][0]
    assert ckpt_call[1]["assignment_id"] == 12
    assert ckpt_call[1]["checkpoint_type"] == "completion"


def test_gate_path_with_assignment_finalizes_lifecycle(tmp_path):
    """Pool-managed gate path: assignment_id provided → full lifecycle after completion."""
    tools = RecordingCoderTools()
    adapter = make_adapter(tools)

    result = run_tracked_gate_role_path(
        adapter,
        task_id=1799,
        role="validator",
        prompt="Validate.",
        run_id="validator-run",
        branch="task/1799-pool",
        head_commit=FAKE_HEAD,
        cwd=tmp_path,
        env_overrides=fake_env(tmp_path),
        runtime_registry_path=write_runtime_registry(tmp_path),
        assignment_id=13,
    )

    assert result.status == "completed"
    assert result.assignment_finalized is True

    lifecycle_calls = [
        name for name, _ in tools.calls
        if name in ("post_worker_completion_packet", "append_checkpoint", "record_cleanup_evidence", "release_assignment")
    ]
    assert lifecycle_calls == [
        "post_worker_completion_packet",
        "append_checkpoint",
        "record_cleanup_evidence",
        "release_assignment",
    ]

    ckpt_call = [c for c in tools.calls if c[0] == "append_checkpoint"][0]
    assert ckpt_call[1]["assignment_id"] == 13
    assert ckpt_call[1]["checkpoint_type"] == "completion"
    assert json.loads(ckpt_call[1]["payload"])["role"] == "validator"


# ---------------------------------------------------------------------------
# main() notification emission at drain boundary
# ---------------------------------------------------------------------------

def _main_test_adapter(summary, next_action, tools_list):
    """Build a fake adapter for main() notification tests."""
    return make_adapter(
        RecordingWorkflowTools(summary=summary, next_action=next_action)
    )


def test_main_emits_notification_on_done(monkeypatch, capsys):
    tools = RecordingWorkflowTools(
        summary={"task": {"id": 1790, "status": "done"}},
        next_action={"next_action": "done", "reason": "task already complete"},
    )
    # Need notification fake on the same tools object
    sent = []
    def fake_send_user_notification(**kwargs):
        sent.append(kwargs)
        return {"id": 9003}
    tools.mcp_den_send_user_notification = fake_send_user_notification

    def fake_build_adapter(*, project_id, requested_by):
        return make_adapter(tools)

    monkeypatch.setattr("den_hermes.orchestrator.build_mcp_adapter", fake_build_adapter)
    exit_code = main(["--project-id", "den-hermes-bridge", "--task-id", "1790", "--json"])

    assert exit_code == 0
    assert len(sent) == 1
    assert sent[0]["urgency"] == "normal"
    assert sent[0]["metadata"]["final_status"] == "completed"
    assert sent[0]["metadata"]["type"] == "agent_work_complete"


def test_main_emits_notification_on_blocked(monkeypatch, capsys):
    tools = RecordingWorkflowTools(
        summary={"task": {"id": 1790, "status": "blocked"}},
        next_action={"next_action": "blocked", "reason": "dependency not met"},
    )
    sent = []
    def fake_send_user_notification(**kwargs):
        sent.append(kwargs)
        return {"id": 9003}
    tools.mcp_den_send_user_notification = fake_send_user_notification

    def fake_build_adapter(*, project_id, requested_by):
        return make_adapter(tools)

    monkeypatch.setattr("den_hermes.orchestrator.build_mcp_adapter", fake_build_adapter)
    exit_code = main(["--project-id", "den-hermes-bridge", "--task-id", "1790", "--json"])

    assert exit_code == 0
    assert len(sent) == 1
    assert sent[0]["urgency"] == "high"
    assert sent[0]["metadata"]["final_status"] == "blocked"


def test_main_emits_notification_on_failed(monkeypatch, capsys):
    tools = RecordingWorkflowTools(
        summary={"task": {"id": 1790, "status": "failed"}},
        next_action={"next_action": "failed", "reason": "worker crashed"},
    )
    sent = []
    def fake_send_user_notification(**kwargs):
        sent.append(kwargs)
        return {"id": 9003}
    tools.mcp_den_send_user_notification = fake_send_user_notification

    def fake_build_adapter(*, project_id, requested_by):
        return make_adapter(tools)

    monkeypatch.setattr("den_hermes.orchestrator.build_mcp_adapter", fake_build_adapter)
    exit_code = main(["--project-id", "den-hermes-bridge", "--task-id", "1790", "--json"])

    assert exit_code == 0
    assert len(sent) == 1
    assert sent[0]["urgency"] == "high"
    assert sent[0]["metadata"]["final_status"] == "failed"


def test_main_no_notification_on_start_coder(monkeypatch, capsys):
    tools = RecordingWorkflowTools(
        summary={"task": {"id": 1790, "status": "planned"}},
        next_action={"next_action": "start_coder", "reason": "ready"},
    )
    sent = []
    def fake_send_user_notification(**kwargs):
        sent.append(kwargs)
        return {"id": 9003}
    tools.mcp_den_send_user_notification = fake_send_user_notification

    def fake_build_adapter(*, project_id, requested_by):
        return make_adapter(tools)

    monkeypatch.setattr("den_hermes.orchestrator.build_mcp_adapter", fake_build_adapter)
    exit_code = main(["--project-id", "den-hermes-bridge", "--task-id", "1790", "--json"])

    assert exit_code == 0
    assert len(sent) == 0


# ---------------------------------------------------------------------------
# Pool-mode runtime authority tests
# ---------------------------------------------------------------------------


def _fake_resolved_runtime(**overrides):
    """Build a ResolvedRuntime with overridable fields."""
    from den_hermes.runtime_registry import ResolvedRuntime

    defaults = dict(
        schema_version=1,
        registry_id="test-registry",
        registry_path="/tmp/test.yaml",
        registry_fingerprint="sha256:test",
        resolved_at="2026-05-31T00:00:00Z",
        role="coder",
        runtime_id="rt-coder-001",
        substrate="spawned_hermes",
        hermes_binary="hermes",
        profile="spawned-coder",
        provider="openai",
        model="gpt-4o",
        toolsets=("terminal", "file"),
        timeout_seconds=300,
        workdir="/tmp/work",
        run_root="/tmp/runs",
        artifact_filename="completion.json",
        log_filename="worker.log",
        source="den-worker",
        extra_args=(),
        preflight={},
    )
    defaults.update(overrides)
    return ResolvedRuntime(**defaults)


def _fake_coder_artifact(**overrides):
    artifact = {
        "status": "completed",
        "branch": "task/test-pool",
        "head_commit": "a" * 40,
        "tests_run": [{"command": "pytest", "result": "pass"}],
        "summary": "Test",
    }
    artifact.update(overrides)
    return artifact


class TestCoderPathPoolMode:
    def setup_registry(self, tmp_path):
        """Create a minimal runtime registry for testing."""
        registry_path = tmp_path / "runtime-registry.yaml"
        registry_path.write_text("""\
schema_version: 1
registry_id: test-pool-registry
defaults:
  substrate: spawned_hermes
  hermes_binary: hermes
  run_root: /tmp/runs
  artifact_filename: completion.json
  log_filename: worker.log
  profile_required: true
  provider_required: true
  model_required: true
  timeout_seconds: 600
  toolsets: [file]
  workdir: /tmp/work
roles:
  coder:
    runtime_id: coder-pool
    profile: spawned-coder
    provider: openai
    model: gpt-4o
    toolsets: [terminal, file]
    workdir: /tmp/work
    run_root: /tmp/runs
  reviewer:
    runtime_id: reviewer-pool
    profile: spawned-reviewer
    provider: openai
    model: gpt-4o
    toolsets: [terminal, file]
    workdir: /tmp/work
    run_root: /tmp/runs
  validator:
    runtime_id: validator-pool
    profile: spawned-validator
    provider: openai
    model: gpt-4o
    toolsets: [terminal, file]
    workdir: /tmp/work
    run_root: /tmp/runs
  drift_checker:
    runtime_id: drift-checker-pool
    profile: spawned-drift-checker
    provider: openai
    model: gpt-4o
    toolsets: [terminal, file]
    workdir: /tmp/work
    run_root: /tmp/runs
  packet_auditor:
    runtime_id: packet-auditor-pool
    profile: spawned-packet-auditor
    provider: openai
    model: gpt-4o
    toolsets: [terminal, file]
    workdir: /tmp/work
    run_root: /tmp/runs
  project_orchestrator:
    runtime_id: project-orchestrator-pool
    profile: spawned-orchestrator
    provider: openai
    model: gpt-4o
    toolsets: [terminal, file]
    workdir: /tmp/work
    run_root: /tmp/runs
    lease_kind: project_orchestrator
role_aliases:
  orchestrator: project_orchestrator
  pooled_orchestrator: project_orchestrator
""")
        return registry_path

    def test_pool_mode_skips_provider_model_in_worker_launch(self, monkeypatch, tmp_path):
        """pool_mode=True → provider/model are None in run_hermes_worker args."""
        registry = self.setup_registry(tmp_path)
        captured_args = {}

        def fake_worker(**kwargs):
            captured_args.update(kwargs)
            return HermesWorkerResult(status="completed", exit_code=0, stdout="", stderr="",
                                      artifact=_fake_coder_artifact())

        monkeypatch.setattr("den_hermes.orchestrator.run_hermes_worker", fake_worker)
        monkeypatch.setattr("os.environ", {
            "DEN_HERMES_POOL_MEMBER_ID": "pool-coder-01",
            "DEN_HERMES_PROFILE": "spawned-coder",
        })

        tools = RecordingCoderTools()
        adapter = make_adapter(tools)
        result = run_tracked_coder_path(
            adapter,
            task_id=1798,
            prompt="Test",
            run_id="pool-coder-run",
            cwd=str(tmp_path),
            runtime_registry_path=str(registry),
            verify_git=False,
            pool_mode=True,
        )

        assert result.status == "completed"
        assert captured_args["provider"] is None, "provider should be None in pool_mode"
        assert captured_args["model"] is None, "model should be None in pool_mode"
        assert captured_args["profile"] == "spawned-coder", "profile should be preserved in pool_mode"
        assert captured_args["toolsets"] is None, "toolsets should be None in pool_mode"

    def test_one_shot_passes_provider_model_in_worker_launch(self, monkeypatch, tmp_path):
        """pool_mode=False (default) → provider/model are passed as-is."""
        registry = self.setup_registry(tmp_path)
        captured_args = {}

        def fake_worker(**kwargs):
            captured_args.update(kwargs)
            return HermesWorkerResult(status="completed", exit_code=0, stdout="", stderr="",
                                      artifact=_fake_coder_artifact())

        monkeypatch.setattr("den_hermes.orchestrator.run_hermes_worker", fake_worker)
        tools = RecordingCoderTools()
        adapter = make_adapter(tools)
        result = run_tracked_coder_path(
            adapter,
            task_id=1798,
            prompt="Test",
            run_id="one-shot-run",
            cwd=str(tmp_path),
            runtime_registry_path=str(registry),
            verify_git=False,
            pool_mode=False,
        )

        assert result.status == "completed"
        assert captured_args["provider"] is not None, "provider should be passed in one-shot mode"
        assert captured_args["model"] is not None, "model should be passed in one-shot mode"

    def test_pool_mode_drift_blocks_assignment(self, monkeypatch, tmp_path):
        """Pool mode with missing pool identity → blocked result."""
        registry = self.setup_registry(tmp_path)
        monkeypatch.setattr("os.environ", {
            "DEN_HERMES_POOL_MEMBER_ID": "",
            "DEN_HERMES_PROFILE": "",
        })

        tools = RecordingCoderTools()
        adapter = make_adapter(tools)
        result = run_tracked_coder_path(
            adapter,
            task_id=1798,
            prompt="Test",
            run_id="pool-drift-run",
            cwd=str(tmp_path),
            runtime_registry_path=str(registry),
            verify_git=False,
            assignment_id=1,
            pool_mode=True,
        )

        assert result.status == "blocked"
        assert "Pool runtime drift" in (result.error or "")
        assert "DEN_HERMES_POOL_MEMBER_ID" in (result.error or "")
        assert result.assignment_finalized is True

    def test_pool_mode_drift_role_profile_mismatch(self, monkeypatch, tmp_path):
        """Pool mode with role/profile mismatch → blocked result."""
        registry = self.setup_registry(tmp_path)
        monkeypatch.setattr("os.environ", {
            "DEN_HERMES_POOL_MEMBER_ID": "pool-coder-01",
            "DEN_HERMES_PROFILE": "spawned-reviewer",
        })

        tools = RecordingCoderTools()
        adapter = make_adapter(tools)
        result = run_tracked_coder_path(
            adapter,
            task_id=1798,
            prompt="Test",
            run_id="pool-drift-role",
            cwd=str(tmp_path),
            runtime_registry_path=str(registry),
            verify_git=False,
            assignment_id=2,
            pool_mode=True,
        )

        assert result.status == "blocked"
        assert "Pool runtime drift" in (result.error or "")
        assert "spawned-reviewer" in (result.error or "")

    def test_pool_mode_no_drift_proceeds(self, monkeypatch, tmp_path):
        """Pool mode with correct env → normal completion."""
        registry = self.setup_registry(tmp_path)
        captured_args = {}

        def fake_worker(**kwargs):
            captured_args.update(kwargs)
            return HermesWorkerResult(status="completed", exit_code=0, stdout="", stderr="",
                                      artifact=_fake_coder_artifact())

        monkeypatch.setattr("den_hermes.orchestrator.run_hermes_worker", fake_worker)
        monkeypatch.setattr("os.environ", {
            "DEN_HERMES_POOL_MEMBER_ID": "pool-coder-01",
            "DEN_HERMES_PROFILE": "spawned-coder",
        })

        tools = RecordingCoderTools()
        adapter = make_adapter(tools)
        result = run_tracked_coder_path(
            adapter,
            task_id=1798,
            prompt="Test",
            run_id="pool-good-run",
            cwd=str(tmp_path),
            runtime_registry_path=str(registry),
            verify_git=False,
            pool_mode=True,
        )

        assert result.status == "completed"
        # Registration should suppress provider/model in pool mode
        reg_call = tools.calls[1]  # register_worker_run
        assert reg_call[1].get("provider") is None
        assert reg_call[1].get("model") is None
        assert reg_call[1].get("profile") == "spawned-coder"


# ---------------------------------------------------------------------------
# Green-path acceptance tests (Runner corrections #9582)
# ---------------------------------------------------------------------------


class _FakeAdapterForPromotion:
    """Minimal fake adapter for _verify_promotion_head_match tests."""

    def __init__(self, *, worker_status=None, workflow_summary=None):
        self._worker_status = worker_status or {}
        self._workflow_summary = workflow_summary or {}

    def get_worker_run_status(self, **kwargs):
        return self._worker_status

    def get_task_workflow_summary(self, **kwargs):
        return self._workflow_summary


class TestVerifyPromotionHeadMatch:
    """Tests 3 & 4 from plan: promotion head-match gate."""

    def test_heads_match_returns_none(self):
        adapter = _FakeAdapterForPromotion(
            worker_status={"head_commit": "abc123"},
        )
        result = _verify_promotion_head_match(
            adapter, task_id=42, reviewed_head="abc123",
        )
        assert result is None

    def test_heads_mismatch_returns_blocking_reason(self):
        adapter = _FakeAdapterForPromotion(
            worker_status={"head_commit": "def456"},
        )
        result = _verify_promotion_head_match(
            adapter, task_id=42, reviewed_head="abc123", coder_run_id="r1",
        )
        assert result is not None
        assert "promotion blocked" in result
        assert "abc123" in result
        assert "def456" in result

    def test_no_current_head_returns_none(self):
        """If no current head found anywhere, allow promotion (no evidence of drift)."""
        adapter = _FakeAdapterForPromotion(
            worker_status={},
            workflow_summary={},
        )
        result = _verify_promotion_head_match(
            adapter, task_id=42, reviewed_head="abc123",
        )
        assert result is None

    def test_falls_back_to_workflow_summary(self):
        adapter = _FakeAdapterForPromotion(
            worker_status={},
            workflow_summary={"latest_coder_completion": {"head_commit": "abc123"}},
        )
        result = _verify_promotion_head_match(
            adapter, task_id=42, reviewed_head="abc123",
        )
        assert result is None

    def test_looks_good_review_blocks_done_ready_when_current_head_differs(self):
        adapter = _FakeAdapterForPromotion(
            worker_status={"head_commit": "newer-head"},
        )
        result = handle_review_outcome(
            adapter,
            task_id=42,
            review_state={
                "verdict": "looks_good",
                "head_commit": "reviewed-head",
                "coder_run_id": "coder-run",
                "findings": [],
            },
            prompt="unused",
            next_coder_run_id="unused",
        )
        assert result.status == "blocked"
        assert "Reviewed head reviewed-head does not match current head newer-head" in result.reason


class TestCoderPromptRequirements:
    """Test 1: coder prompt includes required fields."""

    def test_includes_known_gaps_and_finding_ids(self):
        prompt = _coder_prompt_with_packet(prompt="Do work", packet_message_id=99)
        assert "known_gaps" in prompt
        assert "claimed_finding_ids" in prompt
        assert "tests_run" in prompt
        assert "project_id" in prompt
        assert "head_commit" in prompt

    def test_includes_packet_message_id(self):
        prompt = _coder_prompt_with_packet(prompt="Do work", packet_message_id=42)
        assert "42" in prompt


class TestReviewerPromptRequirements:
    """Test 2: reviewer prompt includes required fields."""

    def test_includes_verdict_and_findings(self):
        prompt = _reviewer_prompt_with_packet(
            prompt="Review this",
            packet_message_id=88,
            branch="task/123",
            head_commit="abc",
            tests_run=[{"cmd": "pytest", "result": "pass"}],
        )
        assert "verdict" in prompt
        assert "findings" in prompt
        assert "known_gaps" in prompt
        assert "task/123" in prompt
        assert "abc" in prompt

    def test_gate_prompt_includes_verdict_and_evidence(self):
        prompt = _gate_prompt_with_packet(
            prompt="Validate",
            role="validator",
            packet_message_id=77,
            branch="task/456",
            head_commit="def",
        )
        assert "verdict" in prompt
        assert "evidence" in prompt
        assert "known_gaps" in prompt
        assert "VALIDATOR" in prompt


class TestWakeStateProjection:
    """Test 5: wake state projection distinguishes states.

    Critical regression: recorded-but-unclaimed must NOT be reported as
    started/running (Runner correction #3).
    """

    def test_recorded_no_run_is_not_started(self):
        """Recorded delivery with no worker run → recorded_pending_claim."""
        adapter = _FakeAdapterForPromotion(
            workflow_summary={"task": {"status": "in_progress"}},
        )
        result = project_wake_state(adapter, task_id=42)
        assert result.wake_state == "recorded_pending_claim"

    def test_run_registered_no_completion_is_running(self):
        adapter = _FakeAdapterForPromotion(
            workflow_summary={"task": {"status": "in_progress"}},
        )
        adapter.get_latest_worker_completion = lambda **kw: {}
        result = project_wake_state(adapter, task_id=42, run_id="r1")
        assert result.wake_state == "running"

    def test_packet_posted(self):
        adapter = _FakeAdapterForPromotion(
            workflow_summary={"task": {"status": "in_progress"}},
        )
        adapter.get_latest_worker_completion = lambda **kw: {
            "status": "completed",
            "packet_type": "implementation_packet",
        }
        result = project_wake_state(adapter, task_id=42, run_id="r1")
        assert result.wake_state == "packet_posted"
        assert result.latest_packet_type == "implementation_packet"

    def test_reviewed_state(self):
        adapter = _FakeAdapterForPromotion(
            workflow_summary={
                "task": {"status": "review"},
                "current_review_state": {"verdict": "looks_good"},
            },
        )
        adapter.get_latest_worker_completion = lambda **kw: {"status": "completed"}
        result = project_wake_state(adapter, task_id=42, run_id="r1")
        assert result.wake_state == "reviewed"
        assert result.review_verdict == "looks_good"

    def test_released_state(self):
        adapter = _FakeAdapterForPromotion(
            workflow_summary={"task": {"status": "done"}},
        )
        adapter.get_latest_worker_completion = lambda **kw: {"status": "completed"}
        result = project_wake_state(adapter, task_id=42, run_id="r1")
        assert result.wake_state == "released"

    def test_frozen_dataclass(self):
        proj = WakeStateProjection(delivery_request_id=1, wake_state="running")
        with pytest.raises(AttributeError):
            proj.wake_state = "released"


class TestEnrichFinalStatus:
    """Runner correction #2: no undefined variables, concrete assignment_id."""

    def test_explicit_params_no_closure(self):
        result = enrich_final_status(
            project_id="den-hermes-bridge",
            task_id=1801,
            run_id="r1",
            assignment_id=15,
            branch="task/1801",
            head_commit="abc123",
            base_commit="def456",
            tests_run=[{"cmd": "pytest", "result": "pass"}],
            review_round_id=3,
            packet_message_id=9500,
            cleanup_released=True,
        )
        assert result["project_id"] == "den-hermes-bridge"
        assert result["task_id"] == 1801
        assert result["run_id"] == "r1"
        assert result["assignment_id"] == 15
        assert result["branch"] == "task/1801"
        assert result["head_commit"] == "abc123"
        assert result["tests_run"] == [{"cmd": "pytest", "result": "pass"}]
        assert result["review_round_id"] == 3
        assert result["cleanup_state"] == "released"
        assert result["source_refs"] == [
            {"kind": "task", "project_id": "den-hermes-bridge", "task_id": 1801},
            {"kind": "run", "run_id": "r1"},
            {"kind": "assignment", "assignment_id": 15},
        ]

    def test_no_run_no_assignment_minimal(self):
        result = enrich_final_status(
            project_id="test",
            task_id=1,
        )
        assert result["run_id"] is None
        assert result["assignment_id"] is None
        assert result["cleanup_state"] == "pending"
        assert result["source_refs"] == [
            {"kind": "task", "project_id": "test", "task_id": 1},
        ]

    def test_assignment_id_is_integer_not_boolean(self):
        """Runner correction: assignment_id must be the concrete int, not a boolean."""
        result = enrich_final_status(
            project_id="p", task_id=1, assignment_id=42,
        )
        assert result["assignment_id"] == 42
        assert isinstance(result["assignment_id"], int)


# ---------------------------------------------------------------------------
# Lease-aware stop tests
# ---------------------------------------------------------------------------


class LeasingRecordingTools:
    """Recording tools that return active leases for stop testing."""

    def __init__(self, *, active_leases=None, stuck_assignments=None, release_fails=False):
        self.calls: list[tuple[str, dict]] = []
        self._leases = active_leases or []
        self._stuck = stuck_assignments or []
        self._release_fails = release_fails

    def mcp_den_list_active_leases(self, **kwargs):
        self.calls.append(("list_active_leases", kwargs))
        return {
            "leases": self._leases,
            "active_leases": self._leases,
        }

    def mcp_den_get_worker_pool_summary(self, **kwargs):
        self.calls.append(("get_worker_pool_summary", kwargs))
        return {
            "active_assignments": self._stuck,
            "assignments": self._stuck,
        }

    def mcp_den_release_orchestrator_lease(self, **kwargs):
        self.calls.append(("release_orchestrator_lease", kwargs))
        if self._release_fails:
            raise RuntimeError("Lease release failed")
        return {"ok": True, "lease_id": kwargs.get("lease_id")}

    def mcp_den_post_worker_completion_packet(self, **kwargs):
        self.calls.append(("post_worker_completion_packet", kwargs))
        return {"ok": True}

    def mcp_den_append_checkpoint(self, **kwargs):
        self.calls.append(("append_checkpoint", kwargs))
        return {"checkpoint_id": 7001}

    def mcp_den_record_cleanup_evidence(self, **kwargs):
        self.calls.append(("record_cleanup_evidence", kwargs))
        return {"ok": True}

    def mcp_den_release_assignment(self, **kwargs):
        self.calls.append(("release_assignment", kwargs))
        return {"ok": True}


def _lease_stop_adapter(tools):
    return DenWorkflowAdapter(
        tools=tools,
        project_id="goblinbench",
        requested_by="pool-orchestrator-01",
    )


def test_lease_aware_stop_no_leases_no_stuck_children():
    """No leases and no stuck children → no_lease_active."""
    tools = LeasingRecordingTools()
    adapter = _lease_stop_adapter(tools)

    result = lease_aware_stop(adapter, task_id=1752, run_id="stop-run-1")

    assert result.status == "no_lease_active"
    assert result.lease_count == 0
    assert result.reconciled_assignments == 0
    assert "No active project_orchestrator leases" in (result.diagnostic or "")


def test_lease_aware_stop_releases_active_leases():
    """Active leases should be released during stop."""
    tools = LeasingRecordingTools(active_leases=[
        {"lease_id": 4, "public_lease_id": "pool-orchestrator-01:goblinbench:b6f39d95"},
    ])
    adapter = _lease_stop_adapter(tools)

    result = lease_aware_stop(adapter, task_id=1752, run_id="stop-run-2", reason="Reclaiming pool member")

    assert result.status == "released"
    assert result.lease_count == 1
    assert len(result.released_leases) == 1
    assert result.released_leases[0] == "pool-orchestrator-01:goblinbench:b6f39d95"
    assert "Released 1/1 active leases" in (result.diagnostic or "")
    assert "Reclaiming pool member" in (result.diagnostic or "")


def test_lease_aware_stop_cleans_stuck_child_assignments():
    """Stuck launching assignments are cleaned up during lease drain."""
    tools = LeasingRecordingTools(
        active_leases=[
            {"lease_id": 4, "public_lease_id": "pool-orchestrator-01:goblinbench:b6f39d95"},
        ],
        stuck_assignments=[
            {"assignment_id": 57, "run_id": "piw_20260601115732_2313fd08", "role": "coder", "status": "launching"},
        ],
    )
    adapter = _lease_stop_adapter(tools)

    result = lease_aware_stop(adapter, task_id=1752, run_id="stop-run-3")

    assert result.status == "released"
    assert result.reconciled_assignments == 1
    assert result.stuck_assignments_cleaned == [57]
    assert "Cleaned up 1 stuck child assignments" in (result.diagnostic or "")


def test_lease_aware_stop_multiple_stuck_children():
    """Multiple stuck assignments are all cleaned up."""
    tools = LeasingRecordingTools(
        active_leases=[
            {"lease_id": 4, "public_lease_id": "pool-orchestrator-01:goblinbench:b6f39d95"},
        ],
        stuck_assignments=[
            {"assignment_id": 57, "run_id": "run-a", "role": "coder", "status": "launching"},
            {"assignment_id": 58, "run_id": "run-b", "role": "reviewer", "status": "ack"},
        ],
    )
    adapter = _lease_stop_adapter(tools)

    result = lease_aware_stop(adapter, task_id=1752, run_id="stop-run-4")

    assert result.status == "released"
    assert result.reconciled_assignments == 2
    assert result.stuck_assignments_cleaned == [57, 58]


def test_lease_aware_stop_no_leases_but_stuck_children_cleaned():
    """No active leases but stuck children still get cleaned up."""
    tools = LeasingRecordingTools(
        stuck_assignments=[
            {"assignment_id": 57, "run_id": "zombie-run", "role": "coder", "status": "launching"},
        ],
    )
    adapter = _lease_stop_adapter(tools)

    result = lease_aware_stop(adapter, task_id=1752, run_id="stop-run-5")

    assert result.status == "reconciled_stuck"
    assert result.lease_count == 0
    assert result.reconciled_assignments == 1
    assert result.stuck_assignments_cleaned == [57]
    assert "cleaned up 1 stuck child assignments" in (result.diagnostic or "").lower()


def test_lease_aware_stop_lease_release_failure_partial():
    """When a lease release fails, the stop still reconciles children."""
    tools = LeasingRecordingTools(
        active_leases=[
            {"lease_id": 4, "public_lease_id": "pool-orchestrator-01:goblinbench:b6f39d95"},
        ],
        release_fails=True,
    )
    adapter = _lease_stop_adapter(tools)

    result = lease_aware_stop(adapter, task_id=1752, run_id="stop-run-6")

    assert result.status == "drained_with_errors"
    assert result.lease_count == 1
    assert len(result.released_leases) == 0  # release failed
    assert "Lease release errors" in (result.diagnostic or "")


def test_lease_aware_stop_no_leases_no_children_clean_state():
    """Empty state: no leases, no children."""
    tools = LeasingRecordingTools()
    adapter = _lease_stop_adapter(tools)

    result = lease_aware_stop(adapter, task_id=1752, run_id="stop-run-7")

    assert result.status == "no_lease_active"
    assert result.reconciled_assignments == 0
    assert not result.stuck_assignments_cleaned


def test_adapter_check_active_leases_returns_leases():
    """check_active_orchestrator_leases returns structured lease state."""
    tools = LeasingRecordingTools(active_leases=[
        {"lease_id": 4, "public_lease_id": "pool-orchestrator-01:goblinbench:b6f39d95"},
    ])
    adapter = _lease_stop_adapter(tools)

    state = adapter.check_active_orchestrator_leases()

    assert state["lease_count"] == 1
    assert len(state["active_leases"]) == 1
    assert state["active_leases"][0]["lease_id"] == 4


def test_adapter_list_child_assignments_finds_stuck():
    """list_active_child_assignments identifies stuck assignments."""
    tools = LeasingRecordingTools(stuck_assignments=[
        {"assignment_id": 57, "run_id": "run-x", "role": "coder", "status": "launching"},
        {"assignment_id": 58, "run_id": "run-y", "role": "coder", "status": "running"},
    ])
    adapter = _lease_stop_adapter(tools)

    state = adapter.list_active_child_assignments()

    assert state["assignment_count"] == 2
    assert state["stuck_count"] == 1  # only status=launching
    assert state["stuck_assignments"][0]["assignment_id"] == 57


def test_adapter_list_child_assignments_empty():
    """Empty assignment state."""
    tools = LeasingRecordingTools()
    adapter = _lease_stop_adapter(tools)

    state = adapter.list_active_child_assignments()

    assert state["assignment_count"] == 0
    assert state["stuck_count"] == 0


def test_cli_stop_flag_triggers_lease_aware_stop(capsys):
    """--stop flag triggers lease_aware_stop path."""
    adapter_args = {}

    def fake_build(project_id, requested_by):
        adapter_args["project_id"] = project_id
        adapter_args["requested_by"] = requested_by
        return _lease_stop_adapter(LeasingRecordingTools(
            active_leases=[
                {"lease_id": 4, "public_lease_id": "pool-orchestrator-01:goblinbench:b6f39d95"},
            ],
        ))

    import den_hermes.orchestrator as orch
    original_build = orch.build_mcp_adapter
    orch.build_mcp_adapter = fake_build
    try:
        exit_code = orch.main([
            "--project-id", "goblinbench",
            "--task-id", "1752",
            "--stop",
            "--stop-reason", "Test stop",
        ])
    finally:
        orch.build_mcp_adapter = original_build

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "stop:" in out
    assert "released" in out.lower()


def test_diagnostic_guardrails_present():
    """Diagnostic guardrail constants are non-empty and contain key phrases."""
    assert "Do NOT search GitHub/web" in PROJECT_ORCHESTRATOR_DIAGNOSTIC_GUARDRAILS
    assert "Do NOT SSH" in PROJECT_ORCHESTRATOR_DIAGNOSTIC_GUARDRAILS
    assert "sysadmin" in PROJECT_ORCHESTRATOR_DIAGNOSTIC_GUARDRAILS.lower()
    assert "runaway command exploration" in PROJECT_ORCHESTRATOR_DIAGNOSTIC_GUARDRAILS.lower()

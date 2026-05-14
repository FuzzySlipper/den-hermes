import json
import subprocess

import pytest

from den_hermes.orchestrator import (
    DenWorkflowAdapter,
    OrchestratorAction,
    OrchestratorActionType,
    decide_next_action,
    main,
    run_tracked_coder_path,
    run_tracked_reviewer_path,
    handle_review_outcome,
    CoderPathResult,
)
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
    assert [name for name, _ in tools.calls] == [
        "prepare_coder_context_packet",
        "register_worker_run",
        "send_message",
        "post_worker_completion_packet",
    ]
    failure_packet = tools.calls[-1][1]
    assert failure_packet["packet_type"] == "worker_failure_packet"
    assert failure_packet["status"] == "failed"


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

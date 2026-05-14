import json

import pytest

from den_hermes.orchestrator import (
    DenWorkflowAdapter,
    OrchestratorAction,
    OrchestratorActionType,
    decide_next_action,
    main,
)


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

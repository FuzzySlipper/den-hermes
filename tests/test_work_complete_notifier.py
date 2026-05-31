"""Tests for ``den_hermes.work_complete_notifier``."""

import json

import pytest

from den_hermes.orchestrator import (
    DenWorkflowAdapter,
    OrchestratorAction,
    OrchestratorActionType,
    _maybe_emit_drain_notification,
)
from den_hermes.work_complete_notifier import (
    WorkCompleteEmissionGuard,
    WorkCompleteNotification,
    _final_status_for_action,
    emit_work_complete_notification,
)


# ---------------------------------------------------------------------------
# Minimal fake adapter / tools
# ---------------------------------------------------------------------------

class _FakeDenTools:
    """Records ``mcp_den_send_user_notification`` calls."""

    def __init__(self):
        self.user_notifications: list[dict] = []

    def mcp_den_send_user_notification(self, **kwargs):
        self.user_notifications.append(kwargs)
        return {"id": 9003}


def _make_adapter(tools):
    return DenWorkflowAdapter(
        tools=tools,
        project_id="den-hermes-bridge",
        requested_by="den-hermes-runner",
    )


# ---------------------------------------------------------------------------
# _final_status_for_action
# ---------------------------------------------------------------------------

class TestFinalStatusForAction:
    def test_done_maps_to_completed(self):
        assert _final_status_for_action("done") == "completed"

    def test_blocked_maps_to_blocked(self):
        assert _final_status_for_action("blocked") == "blocked"

    def test_failed_maps_to_failed(self):
        assert _final_status_for_action("failed") == "failed"

    def test_non_terminal_returns_none(self):
        assert _final_status_for_action("start_coder") is None
        assert _final_status_for_action("await_reviewer") is None


# ---------------------------------------------------------------------------
# WorkCompleteNotification dataclass
# ---------------------------------------------------------------------------

class TestWorkCompleteNotification:
    def test_fields_match_contract(self):
        n = WorkCompleteNotification(
            agent_identity="den-hermes-runner",
            completion_scope="assigned_queue",
            final_status="completed",
            project_ids=["den-hermes-bridge"],
            task_ids=[1790],
            completed_task_ids=[1790],
            blocked_task_ids=[],
            run_ids=["piw_run_1"],
            source_refs=[{"kind": "task", "project_id": "den-hermes-bridge", "task_id": 1790}],
        )
        assert n.agent_identity == "den-hermes-runner"
        assert n.final_status == "completed"
        assert n.source_refs == [{"kind": "task", "project_id": "den-hermes-bridge", "task_id": 1790}]

    def test_frozen(self):
        n = WorkCompleteNotification(
            agent_identity="x", completion_scope="s", final_status="completed",
            project_ids=[], task_ids=[], completed_task_ids=[], blocked_task_ids=[],
            run_ids=[], source_refs=[],
        )
        with pytest.raises(AttributeError):
            n.final_status = "blocked"


# ---------------------------------------------------------------------------
# emit_work_complete_notification
# ---------------------------------------------------------------------------

class TestEmitWorkCompleteNotification:
    def test_calls_adapter_send_user_notification(self):
        tools = _FakeDenTools()
        adapter = _make_adapter(tools)
        notification = WorkCompleteNotification(
            agent_identity="den-hermes-runner",
            completion_scope="assigned_queue",
            final_status="completed",
            project_ids=["den-hermes-bridge"],
            task_ids=[1790],
            completed_task_ids=[1790],
            blocked_task_ids=[],
            run_ids=[],
            source_refs=[{"kind": "task", "project_id": "den-hermes-bridge", "task_id": 1790}],
        )
        result = emit_work_complete_notification(adapter, notification)
        assert result == {"id": 9003}
        assert len(tools.user_notifications) == 1

    def test_urgency_normal_for_completed(self):
        tools = _FakeDenTools()
        adapter = _make_adapter(tools)
        notification = WorkCompleteNotification(
            agent_identity="den-hermes-runner",
            completion_scope="assigned_queue",
            final_status="completed",
            project_ids=["den-hermes-bridge"],
            task_ids=[1790],
            completed_task_ids=[1790],
            blocked_task_ids=[],
            run_ids=[],
            source_refs=[],
        )
        emit_work_complete_notification(adapter, notification)
        assert tools.user_notifications[0]["urgency"] == "normal"

    def test_urgency_high_for_blocked(self):
        tools = _FakeDenTools()
        adapter = _make_adapter(tools)
        notification = WorkCompleteNotification(
            agent_identity="den-hermes-runner",
            completion_scope="assigned_queue",
            final_status="blocked",
            project_ids=["den-hermes-bridge"],
            task_ids=[1790],
            completed_task_ids=[],
            blocked_task_ids=[1790],
            run_ids=[],
            source_refs=[],
        )
        emit_work_complete_notification(adapter, notification)
        assert tools.user_notifications[0]["urgency"] == "high"

    def test_urgency_high_for_failed(self):
        tools = _FakeDenTools()
        adapter = _make_adapter(tools)
        notification = WorkCompleteNotification(
            agent_identity="den-hermes-runner",
            completion_scope="assigned_queue",
            final_status="failed",
            project_ids=["den-hermes-bridge"],
            task_ids=[1790],
            completed_task_ids=[],
            blocked_task_ids=[1790],
            run_ids=[],
            source_refs=[],
        )
        emit_work_complete_notification(adapter, notification)
        assert tools.user_notifications[0]["urgency"] == "high"

    def test_metadata_has_all_required_keys(self):
        tools = _FakeDenTools()
        adapter = _make_adapter(tools)
        notification = WorkCompleteNotification(
            agent_identity="den-hermes-runner",
            completion_scope="assigned_queue",
            final_status="completed",
            project_ids=["den-hermes-bridge"],
            task_ids=[1790],
            completed_task_ids=[1790],
            blocked_task_ids=[],
            run_ids=["piw_run_1"],
            source_refs=[{"kind": "task", "project_id": "den-hermes-bridge", "task_id": 1790}],
        )
        emit_work_complete_notification(adapter, notification)
        metadata = tools.user_notifications[0]["metadata"]
        assert metadata["type"] == "agent_work_complete"
        assert metadata["notification_class"] == "operator_attention"
        assert metadata["agent_identity"] == "den-hermes-runner"
        assert metadata["completion_scope"] == "assigned_queue"
        assert metadata["final_status"] == "completed"
        assert metadata["project_ids"] == ["den-hermes-bridge"]
        assert metadata["task_ids"] == [1790]
        assert metadata["completed_task_ids"] == [1790]
        assert metadata["blocked_task_ids"] == []
        assert metadata["run_ids"] == ["piw_run_1"]

    def test_source_refs_is_native_array_not_json_string(self):
        """Correction #1 from Runner #9549: source_refs must be a native array."""
        tools = _FakeDenTools()
        adapter = _make_adapter(tools)
        notification = WorkCompleteNotification(
            agent_identity="den-hermes-runner",
            completion_scope="assigned_queue",
            final_status="completed",
            project_ids=["den-hermes-bridge"],
            task_ids=[1790],
            completed_task_ids=[1790],
            blocked_task_ids=[],
            run_ids=[],
            source_refs=[
                {"kind": "task", "project_id": "den-hermes-bridge", "task_id": 1790},
                {"kind": "action", "action_type": "done", "reason": "task complete"},
            ],
        )
        emit_work_complete_notification(adapter, notification)
        metadata = tools.user_notifications[0]["metadata"]
        assert isinstance(metadata["source_refs"], list), (
            f"source_refs must be a native list, got {type(metadata['source_refs'])}"
        )
        assert metadata["source_refs"][0]["kind"] == "task"
        assert metadata["source_refs"][1]["kind"] == "action"

    def test_idempotency_guard_suppresses_duplicate(self):
        """Correction #3 from Runner #9549: local drain-level guard."""
        tools = _FakeDenTools()
        adapter = _make_adapter(tools)
        notification = WorkCompleteNotification(
            agent_identity="den-hermes-runner",
            completion_scope="assigned_queue",
            final_status="completed",
            project_ids=["den-hermes-bridge"],
            task_ids=[1790],
            completed_task_ids=[1790],
            blocked_task_ids=[],
            run_ids=[],
            source_refs=[],
        )
        guard = WorkCompleteEmissionGuard()
        result1 = emit_work_complete_notification(adapter, notification, guard=guard)
        assert result1 == {"id": 9003}
        assert guard.emitted is True

        # Second call suppressed
        result2 = emit_work_complete_notification(adapter, notification, guard=guard)
        assert result2 is None
        assert len(tools.user_notifications) == 1  # Still only one notification


# ---------------------------------------------------------------------------
# _maybe_emit_drain_notification (orchestrator integration)
# ---------------------------------------------------------------------------

class TestMaybeEmitDrainNotification:
    def test_done_action_emits_notification(self):
        tools = _FakeDenTools()
        adapter = _make_adapter(tools)
        action = OrchestratorAction(
            type=OrchestratorActionType.DONE,
            reason="task complete",
        )
        _maybe_emit_drain_notification(adapter, action=action, task_id=1790)
        assert len(tools.user_notifications) == 1
        n = tools.user_notifications[0]
        assert n["urgency"] == "normal"
        assert n["metadata"]["final_status"] == "completed"
        assert n["metadata"]["source_refs"][0]["kind"] == "task"

    def test_blocked_action_emits_notification(self):
        tools = _FakeDenTools()
        adapter = _make_adapter(tools)
        action = OrchestratorAction(
            type=OrchestratorActionType.BLOCKED,
            reason="missing dependency",
        )
        _maybe_emit_drain_notification(adapter, action=action, task_id=1790)
        assert len(tools.user_notifications) == 1
        assert tools.user_notifications[0]["urgency"] == "high"
        assert tools.user_notifications[0]["metadata"]["final_status"] == "blocked"

    def test_failed_action_emits_notification(self):
        tools = _FakeDenTools()
        adapter = _make_adapter(tools)
        action = OrchestratorAction(
            type=OrchestratorActionType.FAILED,
            reason="worker crash",
        )
        _maybe_emit_drain_notification(adapter, action=action, task_id=1790)
        assert len(tools.user_notifications) == 1
        assert tools.user_notifications[0]["urgency"] == "high"
        assert tools.user_notifications[0]["metadata"]["final_status"] == "failed"

    def test_start_coder_action_does_not_emit(self):
        tools = _FakeDenTools()
        adapter = _make_adapter(tools)
        action = OrchestratorAction(
            type=OrchestratorActionType.START_CODER,
            reason="needs implementation",
            role="coder",
        )
        _maybe_emit_drain_notification(adapter, action=action, task_id=1790)
        assert len(tools.user_notifications) == 0

    def test_notification_failure_does_not_propagate(self):
        """Notification emission errors must not crash the orchestrator."""
        class _BrokenTools:
            def mcp_den_send_user_notification(self, **kwargs):
                raise RuntimeError("notification service down")

        adapter = DenWorkflowAdapter(
            tools=_BrokenTools(),
            project_id="den-hermes-bridge",
            requested_by="den-hermes-runner",
        )
        action = OrchestratorAction(
            type=OrchestratorActionType.DONE,
            reason="done",
        )
        # Must not raise
        _maybe_emit_drain_notification(adapter, action=action, task_id=1790)

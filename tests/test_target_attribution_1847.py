"""Tests for target-vs-runtime project attribution (task #1847).

Verifies that Hermes Bridge preserves target work metadata while keeping
runtime/control metadata separate. Covers:
- DenWorkflowAdapter with target_project_id different from bridge project
- Worker launcher target project env vars
- Channels bridge response metadata target/runtime fields
- Activity context target/runtime project separation
- Non-bridge target project coder/reviewer/validator wakes
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from den_hermes.agent_message import AgentMessageResult, DenChannelsAgentMessenger
from den_hermes.channels_bridge import (
    InMemoryWakeStore,
    _reply_metadata,
)
from den_hermes.orchestrator import (
    DenWorkflowAdapter,
    build_mcp_adapter,
    enrich_final_status,
)
from den_hermes.worker_launcher import run_hermes_worker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeTools:
    """Records MCP tool calls for assertion."""

    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def _record(self, name: str, **kwargs: Any) -> dict[str, Any]:
        entry = {"tool": name, **kwargs}
        self.calls.append(entry)
        return {"id": len(self.calls), "status": "ok"}

    def mcp_den_get_task_workflow_summary(self, **kw):
        return self._record("get_task_workflow_summary", **kw)

    def mcp_den_determine_orchestrator_next_action(self, **kw):
        return self._record("determine_orchestrator_next_action", **kw)

    def mcp_den_get_latest_worker_completion(self, **kw):
        return self._record("get_latest_worker_completion", **kw)

    def mcp_den_prepare_coder_context_packet(self, **kw):
        return self._record("prepare_coder_context_packet", **kw)

    def mcp_den_prepare_reviewer_context_packet(self, **kw):
        return self._record("prepare_reviewer_context_packet", **kw)

    def mcp_den_prepare_validator_context_packet(self, **kw):
        return self._record("prepare_validator_context_packet", **kw)

    def mcp_den_request_review(self, **kw):
        return self._record("request_review", **kw)

    def mcp_den_register_worker_run(self, **kw):
        return self._record("register_worker_run", **kw)

    def mcp_den_send_message(self, **kw):
        return self._record("send_message", **kw)

    def mcp_den_post_worker_completion_packet(self, **kw):
        return self._record("post_worker_completion_packet", **kw)

    def mcp_den_get_worker_run_status(self, **kw):
        return self._record("get_worker_run_status", **kw)

    def mcp_den_append_checkpoint(self, **kw):
        return self._record("append_checkpoint", **kw)

    def mcp_den_record_cleanup_evidence(self, **kw):
        return self._record("record_cleanup_evidence", **kw)

    def mcp_den_release_assignment(self, **kw):
        return self._record("release_assignment", **kw)

    def mcp_den_send_user_notification(self, **kw):
        return self._record("send_user_notification", **kw)

    def mcp_den_post_review_findings(self, **kw):
        return self._record("post_review_findings", **kw)

    def mcp_den_set_review_verdict(self, **kw):
        return self._record("set_review_verdict", **kw)

    def mcp_den_list_orchestrator_leases(self, **kw):
        return self._record("list_orchestrator_leases", **kw)

    def mcp_den_list_assignments(self, **kw):
        return self._record("list_assignments", **kw)


# ---------------------------------------------------------------------------
# DenWorkflowAdapter target_project_id tests
# ---------------------------------------------------------------------------

class TestDenWorkflowAdapterTargetAttribution:
    """Verify DenWorkflowAdapter uses target_project_id for task-scoped calls
    and project_id for pool/lease-level calls."""

    def test_work_project_id_returns_target_when_set(self):
        tools = FakeTools()
        adapter = DenWorkflowAdapter(
            tools=tools,
            project_id="den-hermes-bridge",
            requested_by="test-runner",
            target_project_id="goblinbench",
        )
        assert adapter.work_project_id == "goblinbench"

    def test_work_project_id_falls_back_to_project_id(self):
        tools = FakeTools()
        adapter = DenWorkflowAdapter(
            tools=tools,
            project_id="den-hermes-bridge",
            requested_by="test-runner",
        )
        assert adapter.work_project_id == "den-hermes-bridge"

    def test_mark_worker_started_uses_target_project(self):
        tools = FakeTools()
        adapter = DenWorkflowAdapter(
            tools=tools,
            project_id="den-hermes-bridge",
            requested_by="test-runner",
            target_project_id="goblinbench",
        )
        adapter.mark_worker_started(task_id=42, run_id="run-1", role="coder")
        call = tools.calls[0]
        assert call["tool"] == "send_message"
        assert call["project_id"] == "goblinbench"
        assert call["metadata"]["runtime_project_id"] == "den-hermes-bridge"

    def test_mark_worker_completed_uses_target_project(self):
        tools = FakeTools()
        adapter = DenWorkflowAdapter(
            tools=tools,
            project_id="den-hermes-bridge",
            requested_by="test-runner",
            target_project_id="goblinbench",
        )
        artifact = {
            "status": "completed",
            "branch": "task/test",
            "head_commit": "abc123",
            "base_commit": "def456",
            "tests_run": [{"command": "pytest", "result": "passed"}],
        }
        adapter.mark_worker_completed(task_id=42, run_id="run-1", role="coder", artifact=artifact)
        call = tools.calls[0]
        assert call["tool"] == "post_worker_completion_packet"
        assert call["project_id"] == "goblinbench"

    def test_mark_worker_failed_uses_target_project(self):
        tools = FakeTools()
        adapter = DenWorkflowAdapter(
            tools=tools,
            project_id="den-hermes-bridge",
            requested_by="test-runner",
            target_project_id="goblinbench",
        )
        adapter.mark_worker_failed(task_id=42, run_id="run-1", role="coder", error="test failure")
        call = tools.calls[0]
        assert call["tool"] == "post_worker_completion_packet"
        assert call["project_id"] == "goblinbench"

    def test_register_worker_run_uses_target_project(self):
        tools = FakeTools()
        adapter = DenWorkflowAdapter(
            tools=tools,
            project_id="den-hermes-bridge",
            requested_by="test-runner",
            target_project_id="goblinbench",
        )
        adapter.register_worker_run(
            task_id=42, run_id="run-1", role="coder",
        )
        call = tools.calls[0]
        assert call["tool"] == "register_worker_run"
        assert call["project_id"] == "goblinbench"

    def test_get_latest_worker_completion_uses_target_project(self):
        tools = FakeTools()
        adapter = DenWorkflowAdapter(
            tools=tools,
            project_id="den-hermes-bridge",
            requested_by="test-runner",
            target_project_id="goblinbench",
        )
        adapter.get_latest_worker_completion(task_id=42, run_id="run-1")
        call = tools.calls[0]
        assert call["tool"] == "get_latest_worker_completion"
        assert call["project_id"] == "goblinbench"

    def test_determine_next_action_uses_target_project(self):
        tools = FakeTools()
        adapter = DenWorkflowAdapter(
            tools=tools,
            project_id="den-hermes-bridge",
            requested_by="test-runner",
            target_project_id="goblinbench",
        )
        adapter.determine_orchestrator_next_action(task_id=42)
        call = tools.calls[0]
        assert call["tool"] == "determine_orchestrator_next_action"
        assert call["project_id"] == "goblinbench"

    def test_get_worker_run_status_uses_target_project(self):
        tools = FakeTools()
        adapter = DenWorkflowAdapter(
            tools=tools,
            project_id="den-hermes-bridge",
            requested_by="test-runner",
            target_project_id="goblinbench",
        )
        adapter.get_worker_run_status(task_id=42, run_id="run-1")
        call = tools.calls[0]
        assert call["tool"] == "get_worker_run_status"
        assert call["project_id"] == "goblinbench"

    def test_prepare_coder_context_packet_uses_target_project(self):
        tools = FakeTools()
        adapter = DenWorkflowAdapter(
            tools=tools,
            project_id="den-hermes-bridge",
            requested_by="test-runner",
            target_project_id="goblinbench",
        )
        adapter.prepare_coder_context_packet(task_id=42)
        call = tools.calls[0]
        assert call["tool"] == "prepare_coder_context_packet"
        assert call["project_id"] == "goblinbench"

    def test_request_review_uses_target_project(self):
        tools = FakeTools()
        adapter = DenWorkflowAdapter(
            tools=tools,
            project_id="den-hermes-bridge",
            requested_by="test-runner",
            target_project_id="goblinbench",
        )
        adapter.request_review(
            task_id=42,
            branch="task/test",
            head_commit="abc123",
            tests_run=[],
        )
        call = tools.calls[0]
        assert call["tool"] == "request_review"
        assert call["project_id"] == "goblinbench"

    def test_send_user_notification_uses_target_project(self):
        tools = FakeTools()
        adapter = DenWorkflowAdapter(
            tools=tools,
            project_id="den-hermes-bridge",
            requested_by="test-runner",
            target_project_id="goblinbench",
        )
        adapter.send_user_notification(
            content="test notification",
            task_id=42,
            metadata={"type": "test"},
        )
        call = tools.calls[0]
        assert call["tool"] == "send_user_notification"
        assert call["project_id"] == "goblinbench"
        assert call["metadata"]["runtime_project_id"] == "den-hermes-bridge"

    def test_pool_level_queries_use_runtime_project(self):
        """Pool/lease/residency queries use project_id (runtime), not target."""
        tools = FakeTools()
        adapter = DenWorkflowAdapter(
            tools=tools,
            project_id="den-hermes-bridge",
            requested_by="test-runner",
            target_project_id="goblinbench",
        )
        adapter.check_active_orchestrator_leases()
        call = tools.calls[0]
        assert call["tool"] == "list_orchestrator_leases"
        assert call["project_id"] == "den-hermes-bridge"

    def test_post_review_findings_uses_target_project(self):
        tools = FakeTools()
        adapter = DenWorkflowAdapter(
            tools=tools,
            project_id="den-hermes-bridge",
            requested_by="test-runner",
            target_project_id="goblinbench",
        )
        adapter.post_review_findings_and_verdict(
            task_id=42,
            review_request={"review_round_id": 1, "message_id": 100},
            reviewer_run_id="review-run-1",
            verdict="looks_good",
            summary="Approved",
        )
        findings_call = tools.calls[0]
        assert findings_call["tool"] == "post_review_findings"
        assert findings_call["project_id"] == "goblinbench"

    def test_backward_compat_no_target_project(self):
        """When target_project_id is not set, all calls use project_id."""
        tools = FakeTools()
        adapter = DenWorkflowAdapter(
            tools=tools,
            project_id="den-hermes-bridge",
            requested_by="test-runner",
        )
        adapter.mark_worker_started(task_id=42, run_id="run-1", role="coder")
        call = tools.calls[0]
        assert call["project_id"] == "den-hermes-bridge"

    @patch.dict(os.environ, {"DEN_HERMES_MCP_URL": "http://mcp.local/mcp"}, clear=False)
    @patch("den_hermes.orchestrator.McpHttpTools")
    def test_build_mcp_adapter_accepts_target_project(self, mock_tools):
        adapter = build_mcp_adapter(
            project_id="den-hermes-bridge",
            requested_by="test-runner",
            target_project_id="goblinbench",
        )
        assert adapter.project_id == "den-hermes-bridge"
        assert adapter.target_project_id == "goblinbench"
        assert adapter.work_project_id == "goblinbench"


# ---------------------------------------------------------------------------
# Channels bridge reply metadata tests
# ---------------------------------------------------------------------------

class TestChannelsBridgeReplyMetadata:
    """Verify _reply_metadata carries target/runtime attribution."""

    def test_reply_metadata_includes_target_project_id(self):
        delivery = {
            "delivery_request_id": 100,
            "dedupe_key": "dk-1",
            "correlation_id": "corr-1",
            "target": {"project_id": "goblinbench", "agent_identity": "coder-1"},
            "source": {"project_id": "den-hermes-bridge"},
        }
        metadata = _reply_metadata(delivery, run_id="run-1")
        assert metadata["target_project_id"] == "goblinbench"

    def test_reply_metadata_includes_runtime_project_when_different(self):
        delivery = {
            "delivery_request_id": 100,
            "dedupe_key": "dk-1",
            "target": {"project_id": "goblinbench"},
            "source": {"project_id": "den-hermes-bridge"},
        }
        metadata = _reply_metadata(delivery, run_id="run-1")
        assert metadata["target_project_id"] == "goblinbench"
        assert metadata["runtime_project_id"] == "den-hermes-bridge"

    def test_reply_metadata_no_runtime_when_same_project(self):
        delivery = {
            "delivery_request_id": 100,
            "dedupe_key": "dk-1",
            "target": {"project_id": "den-hermes-bridge"},
            "source": {"project_id": "den-hermes-bridge"},
        }
        metadata = _reply_metadata(delivery, run_id="run-1")
        assert metadata["target_project_id"] == "den-hermes-bridge"
        assert "runtime_project_id" not in metadata

    def test_reply_metadata_no_target_when_missing(self):
        delivery = {
            "delivery_request_id": 100,
            "dedupe_key": "dk-1",
            "target": {},
            "source": {},
        }
        metadata = _reply_metadata(delivery, run_id="run-1")
        assert "target_project_id" not in metadata
        assert "runtime_project_id" not in metadata


# ---------------------------------------------------------------------------
# enrich_final_status target attribution tests
# ---------------------------------------------------------------------------

class TestEnrichFinalStatusAttribution:
    """Verify enrich_final_status includes target/runtime fields."""

    def test_includes_target_and_runtime_when_different(self):
        status = enrich_final_status(
            project_id="den-hermes-bridge",
            task_id=42,
            target_project_id="goblinbench",
        )
        assert status["target_project_id"] == "goblinbench"
        assert status["runtime_project_id"] == "den-hermes-bridge"

    def test_omits_target_when_same_as_runtime(self):
        status = enrich_final_status(
            project_id="den-hermes-bridge",
            task_id=42,
            target_project_id="den-hermes-bridge",
        )
        assert "target_project_id" not in status
        assert "runtime_project_id" not in status

    def test_omits_target_when_not_provided(self):
        status = enrich_final_status(
            project_id="den-hermes-bridge",
            task_id=42,
        )
        assert "target_project_id" not in status
        assert "runtime_project_id" not in status


# ---------------------------------------------------------------------------
# Worker launcher target project env var tests
# ---------------------------------------------------------------------------

class TestWorkerLauncherTargetProject:
    """Verify run_hermes_worker passes DEN_TARGET_PROJECT_ID."""

    @patch("den_hermes.worker_launcher.subprocess.run")
    @patch("den_hermes.worker_launcher._validate_artifact_identity", return_value=None)
    @patch("den_hermes.worker_launcher._validate_artifact_shape", return_value=None)
    def test_target_project_env_var_flows_through(
        self, mock_shape, mock_identity, mock_run
    ):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="", stderr=""
        )
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_path = Path(tmpdir) / "completion.json"
            artifact_path.write_text(json.dumps({
                "status": "completed",
                "project_id": "goblinbench",
                "task_id": 42,
                "run_id": "run-1",
                "role": "coder",
                "summary": "test",
                "branch": "task/test",
                "head_commit": "abc123",
                "base_commit": "def456",
                "tests_run": [],
            }))

            run_hermes_worker(
                task_id=42,
                run_id="run-1",
                role="coder",
                project_id="den-hermes-bridge",
                prompt="test prompt",
                expected_artifact=str(artifact_path),
                env_overrides={
                    "DEN_TARGET_PROJECT_ID": "goblinbench",
                    "DEN_RUNTIME_PROJECT_ID": "den-hermes-bridge",
                    "DEN_TARGET_TASK_ID": "42",
                },
            )

            call_kwargs = mock_run.call_args
            env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
            assert env is not None
            assert env.get("DEN_PROJECT_ID") == "den-hermes-bridge"
            assert env.get("DEN_RUNTIME_PROJECT_ID") == "den-hermes-bridge"
            assert env.get("DEN_TARGET_PROJECT_ID") == "goblinbench"
            assert env.get("DEN_TARGET_TASK_ID") == "42"

    @patch("den_hermes.worker_launcher.subprocess.run")
    @patch("den_hermes.worker_launcher._validate_artifact_identity", return_value=None)
    @patch("den_hermes.worker_launcher._validate_artifact_shape", return_value=None)
    def test_no_target_project_env_var_when_not_provided(
        self, mock_shape, mock_identity, mock_run
    ):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="", stderr=""
        )
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_path = Path(tmpdir) / "completion.json"
            artifact_path.write_text(json.dumps({
                "status": "completed",
                "project_id": "den-hermes-bridge",
                "task_id": 42,
                "run_id": "run-1",
                "role": "coder",
                "summary": "test",
                "branch": "task/test",
                "head_commit": "abc123",
                "base_commit": "def456",
                "tests_run": [],
            }))

            run_hermes_worker(
                task_id=42,
                run_id="run-1",
                role="coder",
                project_id="den-hermes-bridge",
                prompt="test prompt",
                expected_artifact=str(artifact_path),
            )

            call_kwargs = mock_run.call_args
            env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
            assert "DEN_TARGET_PROJECT_ID" not in env


# ---------------------------------------------------------------------------
# Cross-project wake tests (agent_message already supports this)
# ---------------------------------------------------------------------------

class TestCrossProjectWakeAttribution:
    """Verify agent message wake carries correct target project attribution
    for non-bridge target projects through shared control channel membership."""

    def test_coder_wake_non_bridge_target(self):
        """A coder wake for goblinbench through den-hermes-bridge channel
        should attribute to goblinbench, not den-hermes-bridge."""
        tools = MagicMock()
        tools.den_channels_get_memberships.return_value = {
            "memberships": {
                "channelId": 5,
                "channelSlug": "den-hermes-bridge",
                "channelKind": "control",
                "projectId": "den-hermes-bridge",
                "members": [
                    {
                        "memberIdentity": "pool-coder-01",
                        "memberType": "agent",
                        "membershipStatus": "active",
                    }
                ],
            }
        }
        tools.den_channels_send_direct_agent_message.return_value = {
            "message_id": 200,
            "delivery_request_id": 300,
        }
        tools.den_channels_get_message.return_value = None
        tools.den_channels_get_events.return_value = None

        messenger = DenChannelsAgentMessenger(tools=tools)
        result = messenger.send_agent_message(
            member_identity="pool-coder-01",
            body="Wake: new coder assignment",
            project_id="den-hermes-bridge",
            target_project_id="goblinbench",
            target_task_id=42,
        )

        assert result.status == "sent"
        assert result.target_project_id == "goblinbench"
        assert result.project_id == "den-hermes-bridge"

        # Verify the send args carry the target project
        send_call = tools.den_channels_send_direct_agent_message.call_args
        assert send_call.kwargs.get("source_project_id") == "goblinbench"
        assert send_call.kwargs.get("target_task_id") == 42

    def test_reviewer_wake_non_bridge_target(self):
        """A reviewer wake for goblinbench through den-hermes-bridge channel."""
        tools = MagicMock()
        tools.den_channels_get_memberships.return_value = {
            "memberships": {
                "channelId": 5,
                "projectId": "den-hermes-bridge",
                "members": [
                    {
                        "memberIdentity": "pool-reviewer-01",
                        "memberType": "agent",
                        "membershipStatus": "active",
                    }
                ],
            }
        }
        tools.den_channels_send_direct_agent_message.return_value = {
            "message_id": 201,
        }
        tools.den_channels_get_message.return_value = None
        tools.den_channels_get_events.return_value = None

        messenger = DenChannelsAgentMessenger(tools=tools)
        result = messenger.send_agent_message(
            member_identity="pool-reviewer-01",
            body="Wake: review request",
            project_id="den-hermes-bridge",
            target_project_id="goblinbench",
            target_task_id=42,
        )

        assert result.status == "sent"
        assert result.target_project_id == "goblinbench"

    def test_validator_wake_non_bridge_target(self):
        """A validator wake for goblinbench through den-hermes-bridge channel."""
        tools = MagicMock()
        tools.den_channels_get_memberships.return_value = {
            "memberships": {
                "channelId": 5,
                "projectId": "den-hermes-bridge",
                "members": [
                    {
                        "memberIdentity": "pool-validator-01",
                        "memberType": "agent",
                        "membershipStatus": "active",
                    }
                ],
            }
        }
        tools.den_channels_send_direct_agent_message.return_value = {
            "message_id": 202,
        }
        tools.den_channels_get_message.return_value = None
        tools.den_channels_get_events.return_value = None

        messenger = DenChannelsAgentMessenger(tools=tools)
        result = messenger.send_agent_message(
            member_identity="pool-validator-01",
            body="Wake: validation request",
            project_id="den-hermes-bridge",
            target_project_id="goblinbench",
            target_task_id=42,
        )

        assert result.status == "sent"
        assert result.target_project_id == "goblinbench"


# ---------------------------------------------------------------------------
# Activity context propagation tests
# ---------------------------------------------------------------------------

class TestActivityContextAttribution:
    """Verify _child_activity_context propagates target/runtime project fields."""

    def test_child_context_carries_target_project(self):
        from den_hermes.orchestrator import _child_activity_context

        parent_context = {
            "gatewayUrl": "http://gateway",
            "channelId": "5",
            "displayBlockId": "100",
            "targetProjectId": "goblinbench",
            "runtimeProjectId": "den-hermes-bridge",
            "taskId": "42",
        }
        result = _child_activity_context(
            role="coder",
            run_id="run-1",
            agent_identity="pool-coder-01",
            explicit_context=parent_context,
        )
        assert result is not None
        assert result.get("targetProjectId") == "goblinbench"
        assert result.get("runtimeProjectId") == "den-hermes-bridge"

    def test_child_context_without_target_project(self):
        from den_hermes.orchestrator import _child_activity_context

        parent_context = {
            "gatewayUrl": "http://gateway",
            "channelId": "5",
            "displayBlockId": "100",
            "projectId": "den-hermes-bridge",
        }
        result = _child_activity_context(
            role="coder",
            run_id="run-1",
            agent_identity="pool-coder-01",
            explicit_context=parent_context,
        )
        assert result is not None
        # projectId should still be present
        assert result.get("projectId") == "den-hermes-bridge"
        # targetProjectId should not be present (no target in parent)
        assert "targetProjectId" not in result

    def test_child_context_can_fill_target_from_env_overrides(self):
        from den_hermes.orchestrator import _child_activity_context

        parent_context = {
            "gatewayUrl": "http://gateway",
            "channelId": "5",
            "displayBlockId": "100",
            "projectId": "den-hermes-bridge",
        }
        result = _child_activity_context(
            role="coder",
            run_id="run-1",
            agent_identity="pool-coder-01",
            explicit_context=parent_context,
            env_overrides={
                "DEN_TARGET_PROJECT_ID": "goblinbench",
                "DEN_RUNTIME_PROJECT_ID": "den-hermes-bridge",
                "DEN_TARGET_TASK_ID": "42",
            },
        )
        assert result is not None
        assert result.get("targetProjectId") == "goblinbench"
        assert result.get("runtimeProjectId") == "den-hermes-bridge"
        assert result.get("taskId") == "42"

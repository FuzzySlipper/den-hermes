"""Tests for ``den_hermes.budget_exhaustion_observer``."""

import pytest

from den_hermes.budget_exhaustion_observer import (
    OPERATOR_NOTIFICATION_ROLES,
    BUDGET_EXHAUSTION_KEYWORDS,
    BudgetExhaustionDeduper,
    BudgetExhaustionEmissionEvidence,
    BudgetExhaustionSignal,
    detect_budget_exhaustion,
    emit_budget_exhaustion_signal,
)
from den_hermes.worker_launcher import HermesWorkerResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _worker_result(
    *,
    status: str = "completed",
    exit_code: int | None = 0,
    error: str | None = None,
) -> HermesWorkerResult:
    return HermesWorkerResult(
        status=status,
        exit_code=exit_code,
        stdout="",
        stderr="",
        error=error,
    )


class _FakeDenTools:
    """Records calls to Den MCP tools."""

    def __init__(self):
        self.worker_failures: list[dict] = []
        self.user_notifications: list[dict] = []

    def mark_worker_failed(self, **kwargs):
        self.worker_failures.append(kwargs)

    def send_user_notification(self, **kwargs):
        self.user_notifications.append(kwargs)
        return {"id": 42001}

    def mcp_den_send_user_notification(self, **kwargs):
        self.user_notifications.append(kwargs)
        return {"id": 42001}


def _make_adapter(tools):
    """Create a minimal adapter-like object with the required methods."""
    return tools  # The fake tools object already has the methods.


# ---------------------------------------------------------------------------
# detect_budget_exhaustion
# ---------------------------------------------------------------------------

class TestDetectBudgetExhaustion:

    def test_returns_none_without_den_context(self):
        result = _worker_result(status="incomplete", error="max_iterations reached")
        assert detect_budget_exhaustion(
            worker_result=result,
            project_id=None,
            task_id=1825,
            run_id="run_1",
            role="coder",
            agent_identity="coder-1",
        ) is None

    def test_returns_none_for_completed_worker(self):
        result = _worker_result(status="completed")
        assert detect_budget_exhaustion(
            worker_result=result,
            project_id="proj",
            task_id=1825,
            run_id="run_1",
            role="coder",
            agent_identity="coder-1",
        ) is None

    def test_keyword_match_detected(self):
        result = _worker_result(
            status="failed",
            exit_code=0,
            error="Agent hit max_iterations limit",
        )
        signal = detect_budget_exhaustion(
            worker_result=result,
            project_id="proj",
            task_id=1825,
            run_id="run_1",
            role="coder",
            agent_identity="coder-1",
        )
        assert signal is not None
        assert signal.detection_method == "keyword_match"
        assert signal.project_id == "proj"
        assert signal.task_id == 1825
        assert signal.run_id == "run_1"
        assert signal.role == "coder"

    def test_iteration_budget_keyword(self):
        result = _worker_result(
            status="failed",
            exit_code=0,
            error="iteration budget exhausted before completion",
        )
        signal = detect_budget_exhaustion(
            worker_result=result,
            project_id="proj",
            task_id=1825,
            run_id="run_2",
            role="reviewer",
            agent_identity="reviewer-1",
        )
        assert signal is not None
        assert signal.detection_method == "keyword_match"

    def test_hermes_max_iterations_summary_keyword(self):
        result = _worker_result(
            status="failed",
            exit_code=0,
            error="I reached the maximum iterations (50) but couldn't summarize.",
        )
        signal = detect_budget_exhaustion(
            worker_result=result,
            project_id="proj",
            task_id=1825,
            run_id="run_max_iterations_summary",
            role="reviewer",
            agent_identity="reviewer-1",
        )
        assert signal is not None
        assert signal.detection_method == "keyword_match"

    def test_incomplete_status_detected(self):
        result = _worker_result(
            status="incomplete",
            exit_code=0,
            error="Missing completion artifact: /tmp/artifact.json",
        )
        signal = detect_budget_exhaustion(
            worker_result=result,
            project_id="proj",
            task_id=1825,
            run_id="run_3",
            role="coder",
            agent_identity="coder-1",
        )
        assert signal is not None
        assert signal.detection_method == "artifact_status"
        assert signal.worker_status == "incomplete"

    def test_missing_artifact_inferred(self):
        result = _worker_result(
            status="failed",
            exit_code=0,
            error="Missing completion artifact: /tmp/run_4/completion.json",
        )
        signal = detect_budget_exhaustion(
            worker_result=result,
            project_id="proj",
            task_id=1825,
            run_id="run_4",
            role="coder",
            agent_identity="coder-1",
        )
        assert signal is not None
        assert signal.detection_method == "missing_artifact_inferred"

    def test_failed_without_keyword_returns_none(self):
        """A generic failure without budget keywords should not be detected."""
        result = _worker_result(
            status="failed",
            exit_code=1,
            error="Hermes worker exited with code 1",
        )
        assert detect_budget_exhaustion(
            worker_result=result,
            project_id="proj",
            task_id=1825,
            run_id="run_5",
            role="coder",
            agent_identity="coder-1",
        ) is None

    def test_timeout_not_detected(self):
        """Timeout is not budget exhaustion."""
        result = _worker_result(
            status="failed",
            exit_code=None,
            error="Hermes worker timed out after 300 seconds",
        )
        assert detect_budget_exhaustion(
            worker_result=result,
            project_id="proj",
            task_id=1825,
            run_id="run_6",
            role="coder",
            agent_identity="coder-1",
        ) is None

    def test_crash_exit_nonzero_not_detected(self):
        """Non-zero exit with generic error should not be detected."""
        result = _worker_result(
            status="failed",
            exit_code=137,
            error="Hermes worker exited with code 137",
        )
        assert detect_budget_exhaustion(
            worker_result=result,
            project_id="proj",
            task_id=1825,
            run_id="run_7",
            role="coder",
            agent_identity="coder-1",
        ) is None

    def test_various_budget_keywords(self):
        """All canonical budget keywords should be detected."""
        for keyword in BUDGET_EXHAUSTION_KEYWORDS:
            result = _worker_result(
                status="failed",
                exit_code=0,
                error=f"Agent stopped: {keyword}",
            )
            signal = detect_budget_exhaustion(
                worker_result=result,
                project_id="proj",
                task_id=1825,
                run_id="run_kw",
                role="coder",
                agent_identity="coder-1",
            )
            assert signal is not None, f"Keyword {keyword!r} not detected"
            assert signal.detection_method == "keyword_match"

    def test_no_project_id_returns_none(self):
        result = _worker_result(status="incomplete")
        assert detect_budget_exhaustion(
            worker_result=result,
            project_id=None,
            task_id=1825,
            run_id="run_8",
            role="coder",
            agent_identity="coder-1",
        ) is None

    def test_no_task_id_returns_none(self):
        result = _worker_result(status="incomplete")
        assert detect_budget_exhaustion(
            worker_result=result,
            project_id="proj",
            task_id=None,
            run_id="run_9",
            role="coder",
            agent_identity="coder-1",
        ) is None

    def test_no_run_id_returns_none(self):
        result = _worker_result(status="incomplete")
        assert detect_budget_exhaustion(
            worker_result=result,
            project_id="proj",
            task_id=1825,
            run_id=None,
            role="coder",
            agent_identity="coder-1",
        ) is None

    def test_no_role_returns_none(self):
        result = _worker_result(status="incomplete")
        assert detect_budget_exhaustion(
            worker_result=result,
            project_id="proj",
            task_id=1825,
            run_id="run_10",
            role=None,
            agent_identity="coder-1",
        ) is None

    def test_agent_identity_defaults_to_role(self):
        result = _worker_result(status="incomplete")
        signal = detect_budget_exhaustion(
            worker_result=result,
            project_id="proj",
            task_id=1825,
            run_id="run_11",
            role="coder",
            agent_identity=None,
        )
        assert signal is not None
        assert signal.agent_identity == "coder"


# ---------------------------------------------------------------------------
# BudgetExhaustionDeduper
# ---------------------------------------------------------------------------

class TestBudgetExhaustionDeduper:

    def test_first_signal_allowed(self):
        deduper = BudgetExhaustionDeduper()
        assert not deduper.is_already_signaled("proj", 1825, "run_1")

    def test_duplicate_suppressed(self):
        deduper = BudgetExhaustionDeduper()
        deduper.record_signaled("proj", 1825, "run_1")
        assert deduper.is_already_signaled("proj", 1825, "run_1")

    def test_different_run_allowed(self):
        deduper = BudgetExhaustionDeduper()
        deduper.record_signaled("proj", 1825, "run_1")
        assert not deduper.is_already_signaled("proj", 1825, "run_2")

    def test_different_task_allowed(self):
        deduper = BudgetExhaustionDeduper()
        deduper.record_signaled("proj", 1825, "run_1")
        assert not deduper.is_already_signaled("proj", 9999, "run_1")

    def test_different_project_allowed(self):
        deduper = BudgetExhaustionDeduper()
        deduper.record_signaled("proj", 1825, "run_1")
        assert not deduper.is_already_signaled("other-proj", 1825, "run_1")


# ---------------------------------------------------------------------------
# emit_budget_exhaustion_signal
# ---------------------------------------------------------------------------

class TestEmitBudgetExhaustionSignal:

    def _signal(self, role: str = "coder") -> BudgetExhaustionSignal:
        return BudgetExhaustionSignal(
            project_id="den-hermes-bridge",
            task_id=1825,
            run_id="shw_run_budget_test",
            role=role,
            detection_method="keyword_match",
            worker_status="failed",
            error_summary="Agent hit max_iterations limit",
            agent_identity=f"den-hermes-{role}",
        )

    def test_posts_failure_packet(self):
        tools = _FakeDenTools()
        adapter = _make_adapter(tools)
        signal = self._signal(role="coder")
        deduper = BudgetExhaustionDeduper()

        evidence = emit_budget_exhaustion_signal(
            adapter=adapter,
            signal=signal,
            deduper=deduper,
        )
        assert evidence.failure_packet_posted is True
        assert len(tools.worker_failures) == 1
        failure = tools.worker_failures[0]
        assert failure["task_id"] == 1825
        assert failure["run_id"] == "shw_run_budget_test"
        assert "budget exhausted" in failure["error"].lower()
        assert failure["failure_category"] == "tool_budget_exhausted"
        assert failure["dedupe_key"] == "shw_run_budget_test:tool_budget_exhausted"
        assert "max_iterations" in failure["recovery_guidance"]

    def test_operator_role_gets_notification(self):
        tools = _FakeDenTools()
        adapter = _make_adapter(tools)
        signal = self._signal(role="runner")
        deduper = BudgetExhaustionDeduper()

        evidence = emit_budget_exhaustion_signal(
            adapter=adapter,
            signal=signal,
            deduper=deduper,
        )
        assert evidence.user_notification_posted is True
        assert len(tools.user_notifications) == 1
        notif = tools.user_notifications[0]
        assert notif["urgency"] == "high"
        metadata = notif["metadata"]
        assert metadata["type"] == "tool_budget_exhausted"
        assert metadata["notification_class"] == "operator_attention"
        assert metadata["task_id"] == 1825
        assert metadata["run_id"] == "shw_run_budget_test"
        assert metadata["role"] == "runner"

    def test_project_orchestrator_gets_notification(self):
        tools = _FakeDenTools()
        adapter = _make_adapter(tools)
        signal = self._signal(role="project_orchestrator")
        deduper = BudgetExhaustionDeduper()

        evidence = emit_budget_exhaustion_signal(
            adapter=adapter,
            signal=signal,
            deduper=deduper,
        )
        assert evidence.user_notification_posted is True
        assert len(tools.user_notifications) == 1

    def test_planner_gets_notification(self):
        tools = _FakeDenTools()
        adapter = _make_adapter(tools)
        signal = self._signal(role="planner")
        deduper = BudgetExhaustionDeduper()

        evidence = emit_budget_exhaustion_signal(
            adapter=adapter,
            signal=signal,
            deduper=deduper,
        )
        assert evidence.user_notification_posted is True

    def test_coder_role_no_notification(self):
        tools = _FakeDenTools()
        adapter = _make_adapter(tools)
        signal = self._signal(role="coder")
        deduper = BudgetExhaustionDeduper()

        evidence = emit_budget_exhaustion_signal(
            adapter=adapter,
            signal=signal,
            deduper=deduper,
        )
        assert evidence.user_notification_posted is False
        assert len(tools.user_notifications) == 0

    def test_reviewer_role_no_notification(self):
        tools = _FakeDenTools()
        adapter = _make_adapter(tools)
        signal = self._signal(role="reviewer")
        deduper = BudgetExhaustionDeduper()

        evidence = emit_budget_exhaustion_signal(
            adapter=adapter,
            signal=signal,
            deduper=deduper,
        )
        assert evidence.user_notification_posted is False

    def test_validator_role_no_notification(self):
        tools = _FakeDenTools()
        adapter = _make_adapter(tools)
        signal = self._signal(role="validator")
        deduper = BudgetExhaustionDeduper()

        evidence = emit_budget_exhaustion_signal(
            adapter=adapter,
            signal=signal,
            deduper=deduper,
        )
        assert evidence.user_notification_posted is False

    def test_dedupe_prevents_duplicate_emission(self):
        tools = _FakeDenTools()
        adapter = _make_adapter(tools)
        signal = self._signal(role="coder")
        deduper = BudgetExhaustionDeduper()

        evidence1 = emit_budget_exhaustion_signal(
            adapter=adapter,
            signal=signal,
            deduper=deduper,
        )
        assert evidence1.failure_packet_posted is True
        assert evidence1.dedupe_suppressed is False

        # Second emission suppressed
        evidence2 = emit_budget_exhaustion_signal(
            adapter=adapter,
            signal=signal,
            deduper=deduper,
        )
        assert evidence2.failure_packet_posted is False
        assert evidence2.dedupe_suppressed is True
        assert len(tools.worker_failures) == 1  # Still only one failure packet

    def test_notification_metadata_structure(self):
        tools = _FakeDenTools()
        adapter = _make_adapter(tools)
        signal = self._signal(role="runner")
        deduper = BudgetExhaustionDeduper()

        emit_budget_exhaustion_signal(
            adapter=adapter,
            signal=signal,
            deduper=deduper,
        )
        notif = tools.user_notifications[0]
        metadata = notif["metadata"]
        # Required metadata keys
        assert "type" in metadata
        assert "notification_class" in metadata
        assert "agent_identity" in metadata
        assert "project_id" in metadata
        assert "task_id" in metadata
        assert "run_id" in metadata
        assert "role" in metadata
        assert "detection_method" in metadata
        assert metadata["type"] == "tool_budget_exhausted"

    def test_notification_content_mentions_budget(self):
        tools = _FakeDenTools()
        adapter = _make_adapter(tools)
        signal = self._signal(role="runner")
        deduper = BudgetExhaustionDeduper()

        emit_budget_exhaustion_signal(
            adapter=adapter,
            signal=signal,
            deduper=deduper,
        )
        content = tools.user_notifications[0]["content"]
        assert "BUDGET EXHAUSTED" in content
        assert "1825" in content
        assert "shw_run_budget_test" in content

    def test_emission_evidence_structure(self):
        tools = _FakeDenTools()
        adapter = _make_adapter(tools)
        signal = self._signal(role="coder")
        deduper = BudgetExhaustionDeduper()

        evidence = emit_budget_exhaustion_signal(
            adapter=adapter,
            signal=signal,
            deduper=deduper,
        )
        assert isinstance(evidence, BudgetExhaustionEmissionEvidence)
        assert evidence.failure_packet_posted is True
        assert evidence.user_notification_posted is False
        assert evidence.dedupe_suppressed is False
        assert evidence.signal is signal

    def test_failure_packet_error_message_includes_details(self):
        tools = _FakeDenTools()
        adapter = _make_adapter(tools)
        signal = self._signal(role="coder")
        deduper = BudgetExhaustionDeduper()

        emit_budget_exhaustion_signal(
            adapter=adapter,
            signal=signal,
            deduper=deduper,
        )
        failure = tools.worker_failures[0]
        error = failure["error"]
        assert "budget exhausted" in error.lower()
        assert "coder" in error
        assert "shw_run_budget_test" in error
        assert "keyword_match" in error

    def test_adapter_failure_does_not_crash(self):
        """If adapter.mark_worker_failed raises, evidence should reflect it."""

        class _BrokenAdapter:
            def mark_worker_failed(self, **kwargs):
                raise RuntimeError("Den is down")

        signal = self._signal(role="coder")
        deduper = BudgetExhaustionDeduper()

        # Must not raise
        evidence = emit_budget_exhaustion_signal(
            adapter=_BrokenAdapter(),
            signal=signal,
            deduper=deduper,
        )
        assert evidence.failure_packet_posted is False

    def test_adapter_notification_failure_does_not_crash(self):
        """If adapter.send_user_notification raises, evidence should reflect it."""

        class _HalfBrokenAdapter:
            def __init__(self):
                self.failures = []

            def mark_worker_failed(self, **kwargs):
                self.failures.append(kwargs)

            def send_user_notification(self, **kwargs):
                raise RuntimeError("Notification service down")

        signal = self._signal(role="runner")
        deduper = BudgetExhaustionDeduper()

        evidence = emit_budget_exhaustion_signal(
            adapter=_HalfBrokenAdapter(),
            signal=signal,
            deduper=deduper,
        )
        assert evidence.failure_packet_posted is True
        assert evidence.user_notification_posted is False


# ---------------------------------------------------------------------------
# Integration smoke test — simulated budget-exhausting Den run
# ---------------------------------------------------------------------------

class TestIntegrationSmoke:
    """Smoke test simulating a small max-turns/budget-exhausting Den run."""

    def test_full_path_operator_role(self):
        """Simulate: operator role worker exhausts budget → detect → emit → verify."""
        # Step 1: Simulate worker result from a budget-exhausted run
        worker = _worker_result(
            status="incomplete",
            exit_code=0,
            error="Agent did not complete: Missing completion artifact: /tmp/artifact.json",
        )

        # Step 2: Detect
        signal = detect_budget_exhaustion(
            worker_result=worker,
            project_id="den-hermes-bridge",
            task_id=1825,
            run_id="shw_smoke_operator_1",
            role="runner",
            agent_identity="den-hermes-runner",
        )
        assert signal is not None
        assert signal.detection_method == "artifact_status"

        # Step 3: Emit
        tools = _FakeDenTools()
        adapter = _make_adapter(tools)
        deduper = BudgetExhaustionDeduper()
        evidence = emit_budget_exhaustion_signal(
            adapter=adapter,
            signal=signal,
            deduper=deduper,
        )

        # Step 4: Verify
        assert evidence.failure_packet_posted is True
        assert evidence.user_notification_posted is True
        assert evidence.dedupe_suppressed is False

        # Failure packet has correct structure
        assert len(tools.worker_failures) == 1
        failure = tools.worker_failures[0]
        assert failure["task_id"] == 1825
        assert failure["run_id"] == "shw_smoke_operator_1"
        assert failure["role"] == "runner"

        # User notification has correct structure
        assert len(tools.user_notifications) == 1
        notif = tools.user_notifications[0]
        assert notif["urgency"] == "high"
        assert notif["metadata"]["type"] == "tool_budget_exhausted"

        # Step 5: Verify dedup — second call suppressed
        evidence2 = emit_budget_exhaustion_signal(
            adapter=adapter,
            signal=signal,
            deduper=deduper,
        )
        assert evidence2.dedupe_suppressed is True
        assert len(tools.worker_failures) == 1
        assert len(tools.user_notifications) == 1

    def test_full_path_worker_role(self):
        """Simulate: narrow worker role exhausts budget → detect → emit → verify NO notification."""
        worker = _worker_result(
            status="failed",
            exit_code=0,
            error="max_iterations reached before task completion",
        )

        signal = detect_budget_exhaustion(
            worker_result=worker,
            project_id="den-hermes-bridge",
            task_id=1825,
            run_id="shw_smoke_coder_1",
            role="coder",
            agent_identity="spawned-coder",
        )
        assert signal is not None
        assert signal.detection_method == "keyword_match"

        tools = _FakeDenTools()
        adapter = _make_adapter(tools)
        deduper = BudgetExhaustionDeduper()
        evidence = emit_budget_exhaustion_signal(
            adapter=adapter,
            signal=signal,
            deduper=deduper,
        )

        # Failure packet posted
        assert evidence.failure_packet_posted is True
        assert len(tools.worker_failures) == 1

        # NO user notification for worker role
        assert evidence.user_notification_posted is False
        assert len(tools.user_notifications) == 0

    def test_no_detection_for_non_den_session(self):
        """Ad hoc Hermes CLI session without Den context should not be detected."""
        worker = _worker_result(
            status="incomplete",
            exit_code=0,
            error="Agent did not complete",
        )

        # No Den context
        signal = detect_budget_exhaustion(
            worker_result=worker,
            project_id=None,
            task_id=None,
            run_id=None,
            role=None,
            agent_identity=None,
        )
        assert signal is None

    def test_all_operator_roles_get_notifications(self):
        """Verify every role in OPERATOR_NOTIFICATION_ROLES gets a notification."""
        for role in OPERATOR_NOTIFICATION_ROLES:
            worker = _worker_result(
                status="incomplete",
                exit_code=0,
                error="Missing completion artifact",
            )
            signal = detect_budget_exhaustion(
                worker_result=worker,
                project_id="den-hermes-bridge",
                task_id=1825,
                run_id=f"shw_smoke_{role}_1",
                role=role,
                agent_identity=f"den-hermes-{role}",
            )
            assert signal is not None, f"Failed to detect for role {role}"

            tools = _FakeDenTools()
            adapter = _make_adapter(tools)
            deduper = BudgetExhaustionDeduper()
            evidence = emit_budget_exhaustion_signal(
                adapter=adapter,
                signal=signal,
                deduper=deduper,
            )
            assert evidence.user_notification_posted is True, (
                f"Role {role} did not get user notification"
            )


# ---------------------------------------------------------------------------
# Constants validation
# ---------------------------------------------------------------------------

class TestConstants:
    def test_operator_roles_are_known(self):
        assert "runner" in OPERATOR_NOTIFICATION_ROLES
        assert "admin" in OPERATOR_NOTIFICATION_ROLES
        assert "planner" in OPERATOR_NOTIFICATION_ROLES
        assert "project_orchestrator" in OPERATOR_NOTIFICATION_ROLES

    def test_worker_roles_not_in_operator_set(self):
        assert "coder" not in OPERATOR_NOTIFICATION_ROLES
        assert "reviewer" not in OPERATOR_NOTIFICATION_ROLES
        assert "validator" not in OPERATOR_NOTIFICATION_ROLES
        assert "drift_checker" not in OPERATOR_NOTIFICATION_ROLES
        assert "packet_auditor" not in OPERATOR_NOTIFICATION_ROLES

    def test_budget_keywords_non_empty(self):
        assert len(BUDGET_EXHAUSTION_KEYWORDS) > 0

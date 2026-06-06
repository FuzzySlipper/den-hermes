"""Comprehensive tests for the persistent pool-worker runtime state machine.

Covers:
- Assignment accept/ack
- Checkpoint posting (interpretation, plan, partial_result)
- Gate wait and checkpoint_response handling
- Approved continuation
- Changes requested / corrections
- Block / fail
- Completion packet
- Cleanup evidence
- Release / quarantine
- Mismatched assignment/run identity rejection
- Pool worker profile guide validation
"""

from __future__ import annotations

import pytest

from den_hermes.pool_runtime import (
    AssignmentPointer,
    CheckpointPayload,
    CheckpointResponse,
    CleanupEvidence,
    CompletionPacket,
    PoolCleanupError,
    PoolRuntimeError,
    PoolRuntimeState,
    PoolWorkerProfileGuide,
    PoolWorkerRuntime,
)


# ---------------------------------------------------------------------------
# Fixtures: standard assignment pointer and runtime
# ---------------------------------------------------------------------------


@pytest.fixture
def assignment() -> AssignmentPointer:
    return AssignmentPointer(
        assignment_id="assign-001",
        task_id=1725,
        run_id="t1725-worker-abcdef",
        role="coder",
        project_id="den-hermes-bridge",
    )


@pytest.fixture
def runtime(assignment: AssignmentPointer) -> PoolWorkerRuntime:
    return PoolWorkerRuntime(
        assignment=assignment,
        worker_id="pool-coder-01",
    )


# ---------------------------------------------------------------------------
# Assignment validation
# ---------------------------------------------------------------------------


class TestAssignmentPointer:

    @pytest.mark.parametrize("field,value,expected", [
        ("assignment_id", "", "assignment_id"),
        ("task_id", 0, "task_id"),
        ("task_id", -1, "task_id"),
        ("run_id", "", "run_id"),
        ("role", "unknown_role", "role"),
        ("role", "spawned-reviewer", "role"),
    ])
    def test_invalid_assignment_rejected(self, field: str, value, expected):
        kwargs = {
            "assignment_id": "assign-001",
            "task_id": 1725,
            "run_id": "run-abc",
            "role": "coder",
        }
        kwargs[field] = value
        with pytest.raises(PoolRuntimeError, match=expected):
            AssignmentPointer(**kwargs).validate()

    def test_valid_assignment_accepted(self):
        ptr = AssignmentPointer(
            assignment_id="assign-001",
            task_id=1725,
            run_id="run-abc",
            role="reviewer",
        )
        ptr.validate()  # should not raise


# ---------------------------------------------------------------------------
# State machine: acknowledge and interpretation checkpoint
# ---------------------------------------------------------------------------


class TestAcknowledge:

    def test_acknowledge_from_pending(self, runtime: PoolWorkerRuntime):
        result = runtime.acknowledge(interpretation_summary="Implement pool runtime")
        assert result.state == PoolRuntimeState.ACKNOWLEDGED
        assert result.worker_id == "pool-coder-01"
        assert result.assignment.assignment_id == "assign-001"
        assert result.last_checkpoint is not None
        assert result.last_checkpoint.type == "assignment_ack"
        assert result.last_checkpoint.content["interpretation_summary"] == "Implement pool runtime"

    def test_acknowledge_requires_summary(self, runtime: PoolWorkerRuntime):
        with pytest.raises(PoolRuntimeError, match="interpretation_summary"):
            runtime.acknowledge(interpretation_summary="")

    def test_acknowledge_from_non_pending_rejected(self, runtime: PoolWorkerRuntime):
        ackd = runtime.acknowledge(interpretation_summary="Test")
        with pytest.raises(PoolRuntimeError, match="allowed states"):
            ackd.acknowledge(interpretation_summary="Again")


class TestPostInterpretation:

    def test_post_interpretation_from_acknowledged(self, runtime: PoolWorkerRuntime):
        ackd = runtime.acknowledge(interpretation_summary="Test")
        result = ackd.post_interpretation(
            accepted_criteria=["CR1", "CR2"],
            non_goals=["NG1"],
        )
        assert result.state == PoolRuntimeState.INTERPRETING
        assert result.last_checkpoint is not None
        assert result.last_checkpoint.type == "interpretation_checkpoint"
        assert result.last_checkpoint.content["accepted_criteria"] == ["CR1", "CR2"]

    def test_post_interpretation_requires_criteria(self, runtime: PoolWorkerRuntime):
        ackd = runtime.acknowledge(interpretation_summary="Test")
        with pytest.raises(PoolRuntimeError, match="accepted_criteria"):
            ackd.post_interpretation(accepted_criteria=[], non_goals=["NG1"])

    def test_post_interpretation_requires_non_goals(self, runtime: PoolWorkerRuntime):
        ackd = runtime.acknowledge(interpretation_summary="Test")
        with pytest.raises(PoolRuntimeError, match="non_goals"):
            ackd.post_interpretation(
                accepted_criteria=["CR1"], non_goals=[]
            )

    def test_post_interpretation_from_wrong_state(self, runtime: PoolWorkerRuntime):
        with pytest.raises(PoolRuntimeError, match="allowed states"):
            runtime.post_interpretation(
                accepted_criteria=["CR1"], non_goals=["NG1"]
            )

    def test_post_interpretation_from_approved(self, runtime: PoolWorkerRuntime):
        """Interpretation can be re-posted after changes_requested returns to
        ACKNOWLEDGED."""
        ackd = runtime.acknowledge(interpretation_summary="Test")
        interp = ackd.post_interpretation(
            accepted_criteria=["CR1"], non_goals=["NG1"]
        )
        response = CheckpointResponse(
            verdict="changes_requested",
            checkpoint_type="interpretation_checkpoint",
            run_id="t1725-worker-abcdef",
            assignment_id="assign-001",
            correction="Add more criteria",
        )
        changes = interp.receive_checkpoint_response(response)
        assert changes.state == PoolRuntimeState.ACKNOWLEDGED
        result = changes.post_interpretation(
            accepted_criteria=["CR1", "CR2", "CR3"],
            non_goals=["NG1", "NG2"],
        )
        assert result.state == PoolRuntimeState.INTERPRETING


# ---------------------------------------------------------------------------
# Gate: checkpoint_response handling
# ---------------------------------------------------------------------------


class TestCheckpointResponse:

    def test_interpretation_approved(self, runtime: PoolWorkerRuntime):
        ackd = runtime.acknowledge(interpretation_summary="Test")
        interp = ackd.post_interpretation(
            accepted_criteria=["CR1"], non_goals=["NG1"]
        )
        response = CheckpointResponse(
            verdict="approved",
            checkpoint_type="interpretation_checkpoint",
            run_id="t1725-worker-abcdef",
            assignment_id="assign-001",
        )
        result = interp.receive_checkpoint_response(response)
        assert result.state == PoolRuntimeState.INTERPRETATION_APPROVED

    def test_plan_approved(self, runtime: PoolWorkerRuntime):
        runtime = _to_plan(runtime)
        response = CheckpointResponse(
            verdict="approved",
            checkpoint_type="plan_checkpoint",
            run_id="t1725-worker-abcdef",
            assignment_id="assign-001",
        )
        result = runtime.receive_checkpoint_response(response)
        assert result.state == PoolRuntimeState.PLAN_APPROVED

    def test_approved_with_correction(self, runtime: PoolWorkerRuntime):
        ackd = runtime.acknowledge(interpretation_summary="Test")
        interp = ackd.post_interpretation(
            accepted_criteria=["CR1"], non_goals=["NG1"]
        )
        response = CheckpointResponse(
            verdict="approved_with_correction",
            checkpoint_type="interpretation_checkpoint",
            run_id="t1725-worker-abcdef",
            assignment_id="assign-001",
            correction="Add edge case coverage",
        )
        result = interp.receive_checkpoint_response(response)
        assert result.state == PoolRuntimeState.INTERPRETATION_APPROVED
        assert result.last_response is not None
        assert result.last_response.correction == "Add edge case coverage"

    def test_changes_requested_returns_to_acknowledged(self, runtime: PoolWorkerRuntime):
        ackd = runtime.acknowledge(interpretation_summary="Test")
        interp = ackd.post_interpretation(
            accepted_criteria=["CR1"], non_goals=["NG1"]
        )
        response = CheckpointResponse(
            verdict="changes_requested",
            checkpoint_type="interpretation_checkpoint",
            run_id="t1725-worker-abcdef",
            assignment_id="assign-001",
            correction="Missing critical acceptance criterion",
        )
        result = interp.receive_checkpoint_response(response)
        assert result.state == PoolRuntimeState.ACKNOWLEDGED

    def test_blocked_verdict_blocks_assignment(self, runtime: PoolWorkerRuntime):
        ackd = runtime.acknowledge(interpretation_summary="Test")
        interp = ackd.post_interpretation(
            accepted_criteria=["CR1"], non_goals=["NG1"]
        )
        response = CheckpointResponse(
            verdict="blocked",
            checkpoint_type="interpretation_checkpoint",
            run_id="t1725-worker-abcdef",
            assignment_id="assign-001",
            correction="Need planner context first",
        )
        result = interp.receive_checkpoint_response(response)
        assert result.state == PoolRuntimeState.BLOCKED
        assert result.error is not None
        assert "Need planner context" in result.error

    def test_changes_requested_on_plan_returns_to_interpretation_approved(
            self, runtime: PoolWorkerRuntime):
        runtime = _to_plan(runtime)
        response = CheckpointResponse(
            verdict="changes_requested",
            checkpoint_type="plan_checkpoint",
            run_id="t1725-worker-abcdef",
            assignment_id="assign-001",
            correction="Use different file structure",
        )
        result = runtime.receive_checkpoint_response(response)
        assert result.state == PoolRuntimeState.INTERPRETATION_APPROVED

    def test_mismatched_run_id_rejected(self, runtime: PoolWorkerRuntime):
        ackd = runtime.acknowledge(interpretation_summary="Test")
        interp = ackd.post_interpretation(
            accepted_criteria=["CR1"], non_goals=["NG1"]
        )
        response = CheckpointResponse(
            verdict="approved",
            checkpoint_type="interpretation_checkpoint",
            run_id="wrong-run-id",
            assignment_id="assign-001",
        )
        with pytest.raises(PoolRuntimeError, match="Run ID mismatch"):
            interp.receive_checkpoint_response(response)

    def test_mismatched_assignment_id_rejected(self, runtime: PoolWorkerRuntime):
        ackd = runtime.acknowledge(interpretation_summary="Test")
        interp = ackd.post_interpretation(
            accepted_criteria=["CR1"], non_goals=["NG1"]
        )
        response = CheckpointResponse(
            verdict="approved",
            checkpoint_type="interpretation_checkpoint",
            run_id="t1725-worker-abcdef",
            assignment_id="wrong-assignment",
        )
        with pytest.raises(PoolRuntimeError, match="Assignment ID mismatch"):
            interp.receive_checkpoint_response(response)

    def test_wrong_checkpoint_type_rejected(self, runtime: PoolWorkerRuntime):
        ackd = runtime.acknowledge(interpretation_summary="Test")
        interp = ackd.post_interpretation(
            accepted_criteria=["CR1"], non_goals=["NG1"]
        )
        response = CheckpointResponse(
            verdict="approved",
            checkpoint_type="plan_checkpoint",  # wrong type — still in interpreting
            run_id="t1725-worker-abcdef",
            assignment_id="assign-001",
        )
        with pytest.raises(PoolRuntimeError, match="allowed states"):
            interp.receive_checkpoint_response(response)

    def test_blocked_needs_input_can_be_resolved(self, runtime: PoolWorkerRuntime):
        ackd = runtime.acknowledge(interpretation_summary="Test")
        blocked = ackd.post_blocked_needs_input(
            blocker_summary="Need clarification on scope",
            blocker_category="needs_runner_decision",
            recovery_guidance="Runner to decide scope",
        )
        assert blocked.state == PoolRuntimeState.BLOCKED_NEEDS_INPUT
        response = CheckpointResponse(
            verdict="approved",
            checkpoint_type="blocked_needs_input",
            run_id="t1725-worker-abcdef",
            assignment_id="assign-001",
            notes="Scope confirmed",
        )
        result = blocked.receive_checkpoint_response(response)
        assert result.state == PoolRuntimeState.ACKNOWLEDGED

    def test_blocked_needs_input_with_changes_requested(self, runtime: PoolWorkerRuntime):
        ackd = runtime.acknowledge(interpretation_summary="Test")
        blocked = ackd.post_blocked_needs_input(
            blocker_summary="Unclear approach",
            blocker_category="needs_runner_decision",
            recovery_guidance="Runner to decide approach",
        )
        response = CheckpointResponse(
            verdict="changes_requested",
            checkpoint_type="blocked_needs_input",
            run_id="t1725-worker-abcdef",
            assignment_id="assign-001",
            correction="Try the simpler approach",
        )
        result = blocked.receive_checkpoint_response(response)
        assert result.state == PoolRuntimeState.ACKNOWLEDGED

    def test_checkpoint_response_validation(self):
        with pytest.raises(PoolRuntimeError, match="verdict"):
            CheckpointResponse(
                verdict="unknown_verdict",
                checkpoint_type="interpretation_checkpoint",
                run_id="run-abc",
                assignment_id="assign-001",
            ).validate()

    def test_correction_required_for_approved_with_correction(self):
        with pytest.raises(PoolRuntimeError, match="correction"):
            CheckpointResponse(
                verdict="approved_with_correction",
                checkpoint_type="interpretation_checkpoint",
                run_id="run-abc",
                assignment_id="assign-001",
            ).validate()

    def test_correction_required_for_changes_requested(self):
        with pytest.raises(PoolRuntimeError, match="correction"):
            CheckpointResponse(
                verdict="changes_requested",
                checkpoint_type="interpretation_checkpoint",
                run_id="run-abc",
                assignment_id="assign-001",
            ).validate()

    def test_empty_run_id_rejected(self):
        with pytest.raises(PoolRuntimeError, match="run_id"):
            CheckpointResponse(
                verdict="approved",
                checkpoint_type="interpretation_checkpoint",
                run_id="",
                assignment_id="assign-001",
            ).validate()

    def test_empty_assignment_id_rejected(self):
        with pytest.raises(PoolRuntimeError, match="assignment_id"):
            CheckpointResponse(
                verdict="approved",
                checkpoint_type="interpretation_checkpoint",
                run_id="run-abc",
                assignment_id="",
            ).validate()


# ---------------------------------------------------------------------------
# Plan checkpoint
# ---------------------------------------------------------------------------


class TestPostPlan:

    def test_post_plan_from_approved(self, runtime: PoolWorkerRuntime):
        runtime = _to_interpretation_approved(runtime)
        result = runtime.post_plan(
            files_to_touch=["den_hermes/pool_runtime.py"],
            approach="Implement state machine",
            validation_plan="Run pytests",
        )
        assert result.state == PoolRuntimeState.PLANNING
        assert result.last_checkpoint is not None
        assert result.last_checkpoint.type == "plan_checkpoint"

    def test_post_plan_requires_files(self, runtime: PoolWorkerRuntime):
        runtime = _to_interpretation_approved(runtime)
        with pytest.raises(PoolRuntimeError, match="files_to_touch"):
            runtime.post_plan(
                files_to_touch=[],
                approach="Test",
                validation_plan="Test",
            )

    def test_post_plan_requires_approach(self, runtime: PoolWorkerRuntime):
        runtime = _to_interpretation_approved(runtime)
        with pytest.raises(PoolRuntimeError, match="approach"):
            runtime.post_plan(
                files_to_touch=["f1"],
                approach="",
                validation_plan="Test",
            )

    def test_post_plan_requires_validation_plan(self, runtime: PoolWorkerRuntime):
        runtime = _to_interpretation_approved(runtime)
        with pytest.raises(PoolRuntimeError, match="validation_plan"):
            runtime.post_plan(
                files_to_touch=["f1"],
                approach="Test",
                validation_plan="",
            )

    def test_post_plan_from_wrong_state(self, runtime: PoolWorkerRuntime):
        with pytest.raises(PoolRuntimeError, match="allowed states"):
            runtime.post_plan(
                files_to_touch=["f1"],
                approach="Test",
                validation_plan="Test",
            )


# ---------------------------------------------------------------------------
# Plan approval -> implementation
# ---------------------------------------------------------------------------


class TestProceedToImplementation:

    def test_proceed_to_implementation(self, runtime: PoolWorkerRuntime):
        runtime = _to_plan_approved(runtime)
        result = runtime.proceed_to_implementation()
        assert result.state == PoolRuntimeState.IMPLEMENTING

    def test_proceed_from_wrong_state(self, runtime: PoolWorkerRuntime):
        with pytest.raises(PoolRuntimeError, match="allowed states"):
            runtime.proceed_to_implementation()


# ---------------------------------------------------------------------------
# Partial result checkpoint
# ---------------------------------------------------------------------------


class TestPartialResult:

    def test_post_partial_result(self, runtime: PoolWorkerRuntime):
        runtime = _to_implementing(runtime)
        result = runtime.post_partial_result(
            vertical_slice_paths=["den_hermes/pool_runtime.py"],
            status="first_section_draft",
            diff_summary="Core state machine done",
        )
        assert result.state == PoolRuntimeState.PARTIAL_RESULT
        assert result.last_checkpoint is not None
        assert result.last_checkpoint.type == "partial_result_checkpoint"

    def test_partial_result_requires_paths(self, runtime: PoolWorkerRuntime):
        runtime = _to_implementing(runtime)
        with pytest.raises(PoolRuntimeError, match="vertical_slice_paths"):
            runtime.post_partial_result(
                vertical_slice_paths=[], status="draft"
            )

    def test_partial_result_requires_status(self, runtime: PoolWorkerRuntime):
        runtime = _to_implementing(runtime)
        with pytest.raises(PoolRuntimeError, match="status"):
            runtime.post_partial_result(
                vertical_slice_paths=["f1"], status=""
            )

    def test_partial_result_approved_continues(self, runtime: PoolWorkerRuntime):
        runtime = _to_implementing(runtime)
        partial = runtime.post_partial_result(
            vertical_slice_paths=["f1"], status="draft"
        )
        response = CheckpointResponse(
            verdict="approved",
            checkpoint_type="partial_result_checkpoint",
            run_id="t1725-worker-abcdef",
            assignment_id="assign-001",
        )
        approved = partial.receive_checkpoint_response(response)
        assert approved.state == PoolRuntimeState.PARTIAL_RESULT_APPROVED
        result = approved.continue_implementation()
        assert result.state == PoolRuntimeState.IMPLEMENTING

    def test_partial_result_changes_requested(self, runtime: PoolWorkerRuntime):
        runtime = _to_implementing(runtime)
        partial = runtime.post_partial_result(
            vertical_slice_paths=["f1"], status="draft"
        )
        response = CheckpointResponse(
            verdict="changes_requested",
            checkpoint_type="partial_result_checkpoint",
            run_id="t1725-worker-abcdef",
            assignment_id="assign-001",
            correction="Add more error handling",
        )
        result = partial.receive_checkpoint_response(response)
        assert result.state == PoolRuntimeState.IMPLEMENTING


# ---------------------------------------------------------------------------
# Terminal states: blocked / failed
# ---------------------------------------------------------------------------


class TestBlocked:

    def test_block_from_implementing(self, runtime: PoolWorkerRuntime):
        runtime = _to_implementing(runtime)
        result = runtime.block(reason="Dependency not ready")
        assert result.state == PoolRuntimeState.BLOCKED
        assert result.error is not None
        assert "Dependency not ready" in result.error

    def test_block_requires_reason(self, runtime: PoolWorkerRuntime):
        with pytest.raises(PoolRuntimeError, match="reason"):
            runtime.block(reason="")

    def test_block_from_terminal_state_rejected(self, runtime: PoolWorkerRuntime):
        impl = _to_implementing(runtime)
        blocked = impl.block(reason="Blocked")
        with pytest.raises(PoolRuntimeError, match="Cannot block"):
            blocked.block(reason="Again")

    def test_block_validates_run_id(self, runtime: PoolWorkerRuntime):
        with pytest.raises(PoolRuntimeError, match="Run ID mismatch"):
            runtime.block(reason="Reason", run_id="wrong-run")


class TestFailed:

    def test_fail_from_pending(self, runtime: PoolWorkerRuntime):
        result = runtime.fail(reason="Infrastructure outage")
        assert result.state == PoolRuntimeState.FAILED
        assert result.error is not None
        assert "Infrastructure outage" in result.error

    def test_fail_from_implementing(self, runtime: PoolWorkerRuntime):
        runtime = _to_implementing(runtime)
        result = runtime.fail(reason="Core lease expired")
        assert result.state == PoolRuntimeState.FAILED

    def test_fail_requires_reason(self, runtime: PoolWorkerRuntime):
        with pytest.raises(PoolRuntimeError, match="reason"):
            runtime.fail(reason="")

    def test_fail_from_terminal_state_rejected(self, runtime: PoolWorkerRuntime):
        failed = runtime.fail(reason="Failed")
        with pytest.raises(PoolRuntimeError, match="Cannot fail"):
            failed.fail(reason="Again")

    def test_fail_validates_run_id(self, runtime: PoolWorkerRuntime):
        with pytest.raises(PoolRuntimeError, match="Run ID mismatch"):
            runtime.fail(reason="Reason", run_id="wrong-run")


# ---------------------------------------------------------------------------
# Completion packet
# ---------------------------------------------------------------------------


class TestComplete:

    def test_complete_from_implementing(self, runtime: PoolWorkerRuntime):
        runtime = _to_implementing(runtime)
        packet = CompletionPacket(
            status="completed",
            run_id="t1725-worker-abcdef",
            role="coder",
            task_id=1725,
            summary="Implement pool runtime",
            evidence={"branch": "task/1725-pool-runtime", "head_commit": "abc123"},
        )
        result = runtime.complete(packet)
        assert result.state == PoolRuntimeState.COMPLETING
        assert result.completion is not None
        assert result.completion.summary == "Implement pool runtime"

    def test_finalize_completion(self, runtime: PoolWorkerRuntime):
        runtime = _to_implementing(runtime)
        packet = CompletionPacket(
            status="completed",
            run_id="t1725-worker-abcdef",
            role="coder",
            task_id=1725,
            summary="Done",
        )
        completing = runtime.complete(packet)
        result = completing.finalize_completion()
        assert result.state == PoolRuntimeState.COMPLETED

    def test_complete_from_wrong_state(self, runtime: PoolWorkerRuntime):
        packet = CompletionPacket(
            status="completed",
            run_id="t1725-worker-abcdef",
            role="coder",
            task_id=1725,
            summary="Done",
        )
        with pytest.raises(PoolRuntimeError, match="allowed states"):
            runtime.complete(packet)

    def test_complete_wrong_run_id_fail_closed(self, runtime: PoolWorkerRuntime):
        runtime = _to_implementing(runtime)
        packet = CompletionPacket(
            status="completed",
            run_id="wrong-run-id",
            role="coder",
            task_id=1725,
            summary="Done",
        )
        with pytest.raises(PoolRuntimeError, match="run_id mismatch"):
            runtime.complete(packet)

    def test_complete_wrong_role_fail_closed(self, runtime: PoolWorkerRuntime):
        runtime = _to_implementing(runtime)
        packet = CompletionPacket(
            status="completed",
            run_id="t1725-worker-abcdef",
            role="reviewer",  # wrong role
            task_id=1725,
            summary="Done",
        )
        with pytest.raises(PoolRuntimeError, match="role mismatch"):
            runtime.complete(packet)

    def test_complete_wrong_task_id_fail_closed(self, runtime: PoolWorkerRuntime):
        runtime = _to_implementing(runtime)
        packet = CompletionPacket(
            status="completed",
            run_id="t1725-worker-abcdef",
            role="coder",
            task_id=9999,  # wrong task
            summary="Done",
        )
        with pytest.raises(PoolRuntimeError, match="task_id mismatch"):
            runtime.complete(packet)


# ---------------------------------------------------------------------------
# Cleanup evidence, release, and quarantine
# ---------------------------------------------------------------------------


class TestCleanupAndRelease:

    def test_cleanup_with_complete_evidence(self, runtime: PoolWorkerRuntime):
        runtime = _to_completed(runtime)
        evidence = CleanupEvidence(
            scrub_workspace=True,
            process_release=True,
            session_rotation=True,
            scratch_cleanup=True,
        )
        result = runtime.cleanup(evidence)
        assert result.state == PoolRuntimeState.CLEANED_UP
        assert result.cleanup_evidence is not None
        assert result.cleanup_evidence.is_complete()

    def test_cleanup_incomplete_evidence_quarantines(self, runtime: PoolWorkerRuntime):
        runtime = _to_completed(runtime)
        evidence = CleanupEvidence(
            scrub_workspace=True,
            process_release=False,
            session_rotation=False,
            scratch_cleanup=False,
        )
        with pytest.raises(PoolCleanupError, match="Cleanup evidence incomplete"):
            runtime.cleanup(evidence)

    def test_cleanup_from_non_terminal_rejected(self, runtime: PoolWorkerRuntime):
        runtime = _to_implementing(runtime)
        evidence = CleanupEvidence(
            scrub_workspace=True,
            process_release=True,
            session_rotation=True,
            scratch_cleanup=True,
        )
        with pytest.raises(PoolRuntimeError, match="non-terminal"):
            runtime.cleanup(evidence)

    def test_release_from_cleaned_up(self, runtime: PoolWorkerRuntime):
        runtime = _to_cleaned_up(runtime)
        result = runtime.release()
        assert result.state == PoolRuntimeState.RELEASED

    def test_release_from_wrong_state(self, runtime: PoolWorkerRuntime):
        with pytest.raises(PoolRuntimeError, match="allowed states"):
            runtime.release()

    def test_quarantine_from_terminal_state(self, runtime: PoolWorkerRuntime):
        runtime = _to_completed(runtime)
        result = runtime.quarantine()
        assert result.state == PoolRuntimeState.QUARANTINED

    def test_quarantine_from_non_terminal_rejected(self, runtime: PoolWorkerRuntime):
        with pytest.raises(PoolRuntimeError, match="terminal state"):
            runtime.quarantine()

    def test_can_accept_assignments(self, runtime: PoolWorkerRuntime):
        assert runtime.can_accept_assignments() is False
        cleaned = _to_cleaned_up(runtime)
        assert cleaned.can_accept_assignments() is False
        released = cleaned.release()
        assert released.can_accept_assignments() is True

    def test_cleanup_evidence_missing_fields(self):
        evidence = CleanupEvidence(scrub_workspace=True)
        assert evidence.is_complete() is False
        assert "process_release" in evidence.missing_fields()
        assert "session_rotation" in evidence.missing_fields()
        assert "scratch_cleanup" in evidence.missing_fields()

    def test_cleanup_evidence_complete(self):
        evidence = CleanupEvidence(
            scrub_workspace=True,
            process_release=True,
            session_rotation=True,
            scratch_cleanup=True,
        )
        assert evidence.is_complete() is True
        assert evidence.missing_fields() == frozenset()

    def test_quarantine_required(self, runtime: PoolWorkerRuntime):
        runtime = _to_completed(runtime)
        assert runtime.quarantine_required() is True
        cleaned = runtime.cleanup(CleanupEvidence(
            scrub_workspace=True, process_release=True,
            session_rotation=True, scratch_cleanup=True,
        ))
        assert cleaned.quarantine_required() is False


# ---------------------------------------------------------------------------
# Blocked needs input checkpoint
# ---------------------------------------------------------------------------


class TestBlockedNeedsInput:

    def test_blocked_needs_input_requires_summary(self, runtime: PoolWorkerRuntime):
        with pytest.raises(PoolRuntimeError, match="blocker_summary"):
            runtime.post_blocked_needs_input(
                blocker_summary="",
                blocker_category="needs_runner_decision",
                recovery_guidance="Runner to decide",
            )

    def test_blocked_needs_input_requires_category(self, runtime: PoolWorkerRuntime):
        with pytest.raises(PoolRuntimeError, match="blocker_category"):
            runtime.post_blocked_needs_input(
                blocker_summary="Test",
                blocker_category="",
                recovery_guidance="Runner to decide",
            )

    def test_blocked_needs_input_requires_recovery(self, runtime: PoolWorkerRuntime):
        with pytest.raises(PoolRuntimeError, match="recovery_guidance"):
            runtime.post_blocked_needs_input(
                blocker_summary="Test",
                blocker_category="infrastructure",
                recovery_guidance="",
            )

    def test_blocked_needs_input_from_non_blockable_state_rejected(
            self, runtime: PoolWorkerRuntime):
        completed = _to_completed(runtime)
        with pytest.raises(PoolRuntimeError, match="allowed states"):
            completed.post_blocked_needs_input(
                blocker_summary="Test",
                blocker_category="infrastructure",
                recovery_guidance="Restart",
            )


# ---------------------------------------------------------------------------
# CheckpointPayload validation
# ---------------------------------------------------------------------------


class TestCheckpointPayload:

    def test_valid_checkpoint_types(self):
        for cp_type in [
            "assignment_ack",
            "interpretation_checkpoint",
            "plan_checkpoint",
            "checkpoint_response",
            "partial_result_checkpoint",
            "blocked_needs_input",
        ]:
            payload = CheckpointPayload(
                type=cp_type,
                assignment_id="assign-001",
                run_id="run-abc",
                role="coder",
                task_id=1725,
            )
            payload.validate()  # should not raise

    def test_unknown_checkpoint_type_rejected(self):
        payload = CheckpointPayload(
            type="unknown_type",
            assignment_id="assign-001",
            run_id="run-abc",
            role="coder",
            task_id=1725,
        )
        with pytest.raises(PoolRuntimeError, match="Unknown checkpoint"):
            payload.validate()


# ---------------------------------------------------------------------------
# Status summary
# ---------------------------------------------------------------------------


class TestStatusSummary:

    def test_status_summary_pending(self, runtime: PoolWorkerRuntime):
        summary = runtime.status_summary()
        assert "PENDING" in summary or "pending" in summary
        assert "pool-coder-01" in summary
        assert "assign-001" in summary

    def test_status_summary_completed(self, runtime: PoolWorkerRuntime):
        completed = _to_completed(runtime)
        summary = completed.status_summary()
        assert "completed" in summary.lower()
        assert "summary=Done" in summary

    def test_status_summary_error(self, runtime: PoolWorkerRuntime):
        failed = runtime.fail(reason="Infra failure")
        summary = failed.status_summary()
        assert "failed" in summary.lower()
        assert "Infra failure" in summary


# ---------------------------------------------------------------------------
# Pool worker profile guide
# ---------------------------------------------------------------------------


class TestPoolWorkerProfileGuide:

    def test_valid_profile_guide(self):
        guide = PoolWorkerProfileGuide(
            role="coder",
            runtime_id="coder-pool-1",
            profile="den-pool-coder",
            provider="openai-codex",
            model="gpt-5.1",
            toolsets=("terminal", "file"),
            timeout_seconds=900,
        )
        guide.validate()  # should not raise

    def test_invalid_role(self):
        guide = PoolWorkerProfileGuide(
            role="unknown",
            runtime_id="r1",
            profile="p1",
            provider="pv1",
            model="m1",
        )
        with pytest.raises(PoolRuntimeError, match="role"):
            guide.validate()

    def test_empty_runtime_id(self):
        guide = PoolWorkerProfileGuide(
            role="coder",
            runtime_id="",
            profile="p1",
            provider="pv1",
            model="m1",
        )
        with pytest.raises(PoolRuntimeError, match="runtime_id"):
            guide.validate()

    def test_invalid_cleanup_policy(self):
        guide = PoolWorkerProfileGuide(
            role="coder",
            runtime_id="r1",
            profile="p1",
            provider="pv1",
            model="m1",
            cleanup_policy="aggressive",
        )
        with pytest.raises(PoolRuntimeError, match="cleanup_policy"):
            guide.validate()

    @pytest.mark.parametrize("policy", ["full", "minimal", "manual"])
    def test_valid_cleanup_policies(self, policy):
        guide = PoolWorkerProfileGuide(
            role="reviewer",
            runtime_id="r1",
            profile="p1",
            provider="pv1",
            model="m1",
            cleanup_policy=policy,
        )
        guide.validate()  # should not raise

    def test_empty_profile(self):
        guide = PoolWorkerProfileGuide(
            role="coder",
            runtime_id="r1",
            profile="",
            provider="pv1",
            model="m1",
        )
        with pytest.raises(PoolRuntimeError, match="profile"):
            guide.validate()

    def test_default_toolsets_and_timeout(self):
        guide = PoolWorkerProfileGuide(
            role="validator",
            runtime_id="v1",
            profile="p1",
            provider="pv1",
            model="m1",
        )
        guide.validate()
        assert guide.toolsets == ("file", "terminal")
        assert guide.timeout_seconds == 600

    def test_allowed_checkpoint_types_default(self):
        guide = PoolWorkerProfileGuide(
            role="coder",
            runtime_id="r1",
            profile="p1",
            provider="pv1",
            model="m1",
        )
        assert "interpretation_checkpoint" in guide.allowed_checkpoint_types
        assert "assignment_ack" in guide.allowed_checkpoint_types


# ---------------------------------------------------------------------------
# Diagnostic taxonomy, post-terminal busy detection, membership preflight
# ---------------------------------------------------------------------------

from den_hermes.pool_runtime import PoolMemberDiagnostic, PostTerminalBusyLeak  # noqa: E402


class TestDiagnosticTaxonomy:

    def test_canonical_failure_categories_recognized(self):
        """All canonical categories are defined and have non-empty descriptions."""
        canon = PoolMemberDiagnostic.canonical_failure_categories()
        assert "membership_not_active" in canon
        assert "wake_route_404" in canon
        assert "auth_unhealthy" in canon
        assert "post_terminal_pool_state_leak" in canon
        assert len(canon) >= 4
        for category, description in canon.items():
            assert isinstance(category, str) and category
            assert isinstance(description, str) and description

    def test_create_diagnostic_with_known_category(self):
        diag = PoolMemberDiagnostic(
            category="membership_not_active",
            member_id="pool-packet-auditor-03",
            evidence={"channel_id": 672, "membership_status": "inactive"},
            recovery="Restore active membership for spawned-packet-auditor in #den-system",
        )
        assert diag.category == "membership_not_active"
        assert diag.severity == "critical"
        assert "pool-packet-auditor-03" in str(diag)

    def test_create_diagnostic_with_auth_category(self):
        diag = PoolMemberDiagnostic(
            category="auth_unhealthy",
            member_id="pool-packet-auditor-03",
            evidence={"auth_status": "expired", "provider": "openai"},
            recovery="Refresh OAuth token or rotate API key",
        )
        assert diag.category == "auth_unhealthy"
        assert diag.severity == "critical"

    def test_unknown_category_raises(self):
        with pytest.raises(ValueError, match="Unknown failure category"):
            PoolMemberDiagnostic(
                category="bogus_reason",
                member_id="worker-1",
                evidence={},
                recovery="fix it",
            )

    def test_diagnostic_summary_includes_all_fields(self):
        diag = PoolMemberDiagnostic(
            category="wake_route_404",
            member_id="pool-packet-auditor-03",
            evidence={"route": "/api/direct-agent-events"},
            recovery="Check channel membership",
        )
        text = diag.summary()
        assert "wake_route_404" in text
        assert "pool-packet-auditor-03" in text
        assert "Check channel membership" in text


class TestPostTerminalBusyDetection:

    def test_completed_no_active_assignment_detected_as_leak(self):
        """A runtime in COMPLETED with no active assignment is a post-terminal leak."""
        assignment = AssignmentPointer(
            assignment_id="assign-001",
            task_id=2071,
            run_id="piw_test",
            role="packet_auditor",
            project_id="den-core",
        )
        runtime = PoolWorkerRuntime(assignment=assignment, worker_id="pool-packet-auditor-03",
                                    state=PoolRuntimeState.COMPLETED)
        leak = runtime.detect_post_terminal_busy(active_assignment_count=0)
        assert leak is not None
        assert leak.member_id == "pool-packet-auditor-03"
        assert leak.category == "post_terminal_pool_state_leak"
        assert "pool-packet-auditor-03" in str(leak)
        assert "completed" in str(leak)
        assert "active_assignments=0" in str(leak)

    def test_completed_with_active_assignment_no_leak(self):
        """A runtime in COMPLETED but with an active assignment is fine."""
        assignment = AssignmentPointer(
            assignment_id="assign-001",
            task_id=2071,
            run_id="piw_test",
            role="packet_auditor",
        )
        runtime = PoolWorkerRuntime(assignment=assignment, worker_id="pool-packet-auditor-03",
                                    state=PoolRuntimeState.COMPLETED)
        leak = runtime.detect_post_terminal_busy(active_assignment_count=1)
        assert leak is None

    def test_non_terminal_state_not_a_leak(self):
        """A non-terminal state should never be flagged as a leak."""
        assignment = AssignmentPointer(
            assignment_id="assign-001",
            task_id=2071,
            run_id="piw_test",
            role="packet_auditor",
        )
        runtime = PoolWorkerRuntime(assignment=assignment, worker_id="pool-packet-auditor-03",
                                    state=PoolRuntimeState.IMPLEMENTING)
        leak = runtime.detect_post_terminal_busy(active_assignment_count=0)
        assert leak is None

    def test_failed_state_without_active_assignment_leak(self):
        """FAILED with no active assignment should be detected as leak."""
        assignment = AssignmentPointer(
            assignment_id="assign-001",
            task_id=2071,
            run_id="piw_test",
            role="packet_auditor",
        )
        runtime = PoolWorkerRuntime(assignment=assignment, worker_id="pool-packet-auditor-03",
                                    state=PoolRuntimeState.FAILED)
        leak = runtime.detect_post_terminal_busy(active_assignment_count=0)
        assert leak is not None
        assert leak.category == "post_terminal_pool_state_leak"

    def test_released_state_no_active_assignment_not_leak(self):
        """RELEASED with no active assignments is normal — not a leak."""
        assignment = AssignmentPointer(
            assignment_id="assign-001",
            task_id=2071,
            run_id="piw_test",
            role="packet_auditor",
        )
        runtime = PoolWorkerRuntime(assignment=assignment, worker_id="pool-packet-auditor-03",
                                    state=PoolRuntimeState.RELEASED)
        leak = runtime.detect_post_terminal_busy(active_assignment_count=0)
        assert leak is None


class TestProfileGuideMembershipPreflight:

    def test_packet_auditor_requires_channel_membership(self):
        guide = PoolWorkerProfileGuide(
            role="packet_auditor",
            runtime_id="r1",
            profile="spawned-packet-auditor",
            provider="openai",
            model="gpt-5",
            requires_channel_membership=True,
            target_channel_id=672,
        )
        assert guide.requires_channel_membership is True
        assert guide.target_channel_id == 672

    def test_coder_does_not_require_membership_by_default(self):
        guide = PoolWorkerProfileGuide(
            role="coder",
            runtime_id="r1",
            profile="spawned-coder",
            provider="openai",
            model="gpt-5",
        )
        assert guide.requires_channel_membership is False
        assert guide.target_channel_id is None

    def test_membership_preflight_packet_auditor(self):
        """packet_auditor with target_channel_id set requires membership check."""
        guide = PoolWorkerProfileGuide(
            role="packet_auditor",
            runtime_id="r1",
            profile="spawned-packet-auditor",
            provider="openai",
            model="gpt-5",
            requires_channel_membership=True,
            target_channel_id=672,
        )
        assert guide.needs_membership_preflight() is True

    def test_membership_preflight_coder_default(self):
        """coder with default settings does not need membership preflight."""
        guide = PoolWorkerProfileGuide(
            role="coder",
            runtime_id="r1",
            profile="spawned-coder",
            provider="openai",
            model="gpt-5",
        )
        assert guide.needs_membership_preflight() is False

    def test_validate_requires_membership_without_channel_rejected(self):
        """requires_channel_membership=True without target_channel_id is invalid."""
        with pytest.raises(PoolRuntimeError, match="requires.*channel_membership"):
            guide = PoolWorkerProfileGuide(
                role="packet_auditor",
                runtime_id="r1",
                profile="spawned-packet-auditor",
                provider="openai",
                model="gpt-5",
                requires_channel_membership=True,
                target_channel_id=None,
            )
            guide.validate()


# ---------------------------------------------------------------------------
# Helper functions to reach specific states
# ---------------------------------------------------------------------------


from den_hermes.pool_runtime import (  # noqa: E402
    ProfileHealthResult,
    check_profile_health,
    pre_assignment_health_check,
    reconcile_pool_members,
    terminal_cleanup_reconciliation,
)


class TestReconcilePoolMembers:

    def test_detects_completed_member_without_assignment(self):
        members = [
            {"member_id": "pool-packet-auditor-03", "state": "completed",
             "role": "packet_auditor", "assignment_id": "assign-491"},
        ]
        leaks = reconcile_pool_members(members=members, active_assignments_by_member={})
        assert len(leaks) == 1
        assert leaks[0].member_id == "pool-packet-auditor-03"
        assert leaks[0].category == "post_terminal_pool_state_leak"

    def test_skips_member_with_active_assignment(self):
        members = [
            {"member_id": "pool-packet-auditor-03", "state": "completed",
             "role": "packet_auditor"},
        ]
        leaks = reconcile_pool_members(
            members=members,
            active_assignments_by_member={"pool-packet-auditor-03": 1},
        )
        assert len(leaks) == 0

    def test_skips_released_members(self):
        members = [
            {"member_id": "pool-coder-01", "state": "released", "role": "coder"},
        ]
        leaks = reconcile_pool_members(members=members)
        assert len(leaks) == 0

    def test_detects_multiple_leaks(self):
        members = [
            {"member_id": "auditor-1", "state": "failed", "role": "packet_auditor"},
            {"member_id": "coder-1", "state": "completed", "role": "coder"},
            {"member_id": "reviewer-1", "state": "released", "role": "reviewer"},
        ]
        leaks = reconcile_pool_members(members=members)
        assert len(leaks) == 2
        leak_ids = {l.member_id for l in leaks}
        assert leak_ids == {"auditor-1", "coder-1"}


class TestProfileHealthCheck:

    def test_default_health_check_is_healthy(self):
        result = check_profile_health(
            profile="spawned-packet-auditor",
            provider="openai",
            model="gpt-5",
        )
        assert result.is_healthy()
        assert result.to_diagnostic(member_id="test") is None

    def test_unhealthy_check_produces_diagnostic(self):
        def fake_health(_p, _r, _m):
            return (False, "expired OAuth token")

        result = check_profile_health(
            profile="spawned-packet-auditor",
            provider="openai",
            model="gpt-5",
            health_fn=fake_health,
        )
        assert not result.is_healthy()
        assert result.category == "auth_unhealthy"

        diag = result.to_diagnostic(member_id="pool-packet-auditor-03")
        assert diag is not None
        assert diag.category == "auth_unhealthy"
        assert diag.member_id == "pool-packet-auditor-03"
        assert "expired OAuth token" in diag.summary()
        assert "openai" in diag.summary()

    def test_healthy_check_returns_none_diagnostic(self):
        def fake_health(_p, _r, _m):
            return (True, "ok")

        result = check_profile_health(
            profile="spawned-packet-auditor",
            provider="openai",
            model="gpt-5",
            health_fn=fake_health,
        )
        assert result.is_healthy()
        assert result.to_diagnostic(member_id="test") is None


class TestPreAssignmentHealthCheck:

    def test_unhealthy_profile_blocks_assignment(self):
        guide = PoolWorkerProfileGuide(
            role="packet_auditor",
            runtime_id="r1",
            profile="spawned-packet-auditor",
            provider="openai",
            model="gpt-5",
            requires_channel_membership=True,
            target_channel_id=672,
        )

        def fake_health(_p, _r, _m):
            return (False, "expired OAuth token")

        diag = pre_assignment_health_check(
            guide=guide,
            member_id="pool-packet-auditor-03",
            health_fn=fake_health,
        )
        assert diag is not None
        assert diag.category == "auth_unhealthy"
        assert "expired OAuth token" in diag.summary()

    def test_healthy_profile_returns_none(self):
        guide = PoolWorkerProfileGuide(
            role="packet_auditor",
            runtime_id="r1",
            profile="spawned-packet-auditor",
            provider="openai",
            model="gpt-5",
            requires_channel_membership=True,
            target_channel_id=672,
        )

        def fake_health(_p, _r, _m):
            return (True, "healthy")

        diag = pre_assignment_health_check(
            guide=guide,
            member_id="pool-packet-auditor-03",
            health_fn=fake_health,
        )
        assert diag is None


class TestTerminalCleanupReconciliation:
    """Tests for terminal_cleanup_reconciliation (wired reconciliation path)."""

    def test_detects_leak_and_produces_diagnostic(self):
        members = [
            {"member_id": "pool-packet-auditor-03", "state": "completed",
             "role": "packet_auditor", "assignment_id": "assign-491"},
        ]
        leaks, diagnostics = terminal_cleanup_reconciliation(
            members=members,
            active_assignments_by_member={},
        )
        assert len(leaks) == 1
        assert len(diagnostics) == 1
        assert diagnostics[0].category == "post_terminal_pool_state_leak"
        assert "Release or quarantine" in diagnostics[0].recovery

    def test_no_leaks_no_diagnostics(self):
        members = [
            {"member_id": "pool-coder-01", "state": "released", "role": "coder"},
        ]
        leaks, diagnostics = terminal_cleanup_reconciliation(
            members=members,
        )
        assert len(leaks) == 0
        assert len(diagnostics) == 0


class TestWorkerClaimTimeoutCanonical:

    def test_worker_claim_timeout_in_canonical_categories(self):
        canon = PoolMemberDiagnostic.canonical_failure_categories()
        assert "worker_claim_timeout" in canon
        assert "claim" in canon["worker_claim_timeout"].lower()

    def test_create_diagnostic_for_claim_timeout(self):
        diag = PoolMemberDiagnostic(
            category="worker_claim_timeout",
            member_id="pool-packet-auditor-03",
            evidence={"delivery_request_id": 123, "elapsed_seconds": 900},
            recovery="Retry delivery or reassign to another worker",
        )
        assert diag.category == "worker_claim_timeout"
        assert "pool-packet-auditor-03" in diag.summary()


class TestProvisionPreflightDefaults:
    """Tests that provisioning applies role-specific preflight defaults."""

    def test_packet_auditor_preflight_defaults_in_resolve_runtime(self):
        """packet_auditor gets requires_channel_membership=True, target_channel_id=672
        even when the role entry has no preflight section."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
        from provision_pool_workers import resolve_role_runtime, build_pool_member, ROLE_PREFLIGHT_DEFAULTS

        # Simulate a minimal registry where packet_auditor has no preflight
        defaults = {
            "substrate": "spawned_hermes",
            "profile_required": True,
            "provider_required": True,
            "model_required": True,
            "toolsets": [],
            "timeout_seconds": 900,
        }
        roles = {
            "packet_auditor": {
                "runtime_id": "pa-primary",
                "profile": "spawned-packet-auditor",
                "provider": "openai",
                "model": "gpt-5",
                "timeout_seconds": 600,
                # No preflight section — relies on ROLE_PREFLIGHT_DEFAULTS
            },
        }
        registry = {"role_aliases": {}, "schema_version": 1, "defaults": defaults, "roles": roles, "registry_id": "test"}

        runtime = resolve_role_runtime("packet_auditor", registry, defaults, roles)

        # Before applying ROLE_PREFLIGHT_DEFAULTS, values come from registry only
        assert runtime["requires_channel_membership"] is False
        assert runtime["target_channel_id"] is None

        # Apply ROLE_PREFLIGHT_DEFAULTS (this is what run_provision does)
        pfd = ROLE_PREFLIGHT_DEFAULTS.get("packet_auditor", {})
        if pfd:
            for key, value in pfd.items():
                runtime[key] = value

        assert runtime["requires_channel_membership"] is True
        assert runtime["target_channel_id"] == 672

        # Now build the member to verify it carries the fields
        member = build_pool_member("packet_auditor", runtime, slot_number=1)
        assert member.requires_channel_membership is True
        assert member.target_channel_id == 672

    def test_coder_has_no_preflight_defaults(self):
        """coder should NOT have membership preflight defaults."""
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
        from provision_pool_workers import resolve_role_runtime, build_pool_member, ROLE_PREFLIGHT_DEFAULTS

        # coder has no preflight default override
        assert ROLE_PREFLIGHT_DEFAULTS.get("coder") is None

        defaults = {
            "substrate": "spawned_hermes",
            "profile_required": True,
            "provider_required": True,
            "model_required": True,
            "toolsets": [],
            "timeout_seconds": 900,
        }
        roles = {
            "coder": {
                "runtime_id": "coder-primary",
                "profile": "spawned-coder",
                "provider": "openai",
                "model": "gpt-5",
            },
        }
        registry = {"role_aliases": {}, "schema_version": 1, "defaults": defaults, "roles": roles, "registry_id": "test"}
        runtime = resolve_role_runtime("coder", registry, defaults, roles)
        member = build_pool_member("coder", runtime, slot_number=1)

        assert member.requires_channel_membership is False
        assert member.target_channel_id is None


def _to_interpretation_approved(runtime: PoolWorkerRuntime) -> PoolWorkerRuntime:
    ackd = runtime.acknowledge(interpretation_summary="Test")
    interp = ackd.post_interpretation(
        accepted_criteria=["CR1"],
        non_goals=["NG1"],
    )
    response = CheckpointResponse(
        verdict="approved",
        checkpoint_type="interpretation_checkpoint",
        run_id=runtime.assignment.run_id,
        assignment_id=runtime.assignment.assignment_id,
    )
    return interp.receive_checkpoint_response(response)


def _to_plan(runtime: PoolWorkerRuntime) -> PoolWorkerRuntime:
    approved = _to_interpretation_approved(runtime)
    return approved.post_plan(
        files_to_touch=["f1"],
        approach="Implement",
        validation_plan="Run tests",
    )


def _to_plan_approved(runtime: PoolWorkerRuntime) -> PoolWorkerRuntime:
    planning = _to_plan(runtime)
    response = CheckpointResponse(
        verdict="approved",
        checkpoint_type="plan_checkpoint",
        run_id=runtime.assignment.run_id,
        assignment_id=runtime.assignment.assignment_id,
    )
    return planning.receive_checkpoint_response(response)


def _to_implementing(runtime: PoolWorkerRuntime) -> PoolWorkerRuntime:
    plan_approved = _to_plan_approved(runtime)
    return plan_approved.proceed_to_implementation()


def _to_completed(runtime: PoolWorkerRuntime) -> PoolWorkerRuntime:
    impl = _to_implementing(runtime)
    packet = CompletionPacket(
        status="completed",
        run_id=runtime.assignment.run_id,
        role=runtime.assignment.role,
        task_id=runtime.assignment.task_id,
        summary="Done",
    )
    completing = impl.complete(packet)
    return completing.finalize_completion()


def _to_cleaned_up(runtime: PoolWorkerRuntime) -> PoolWorkerRuntime:
    completed = _to_completed(runtime)
    evidence = CleanupEvidence(
        scrub_workspace=True,
        process_release=True,
        session_rotation=True,
        scratch_cleanup=True,
    )
    return completed.cleanup(evidence)

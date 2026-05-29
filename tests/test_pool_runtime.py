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
# Helper functions to reach specific states
# ---------------------------------------------------------------------------


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

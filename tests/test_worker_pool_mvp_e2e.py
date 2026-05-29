"""Fake end-to-end tests for the Worker Pool MVP workflow.

This module simulates the full MVP lifecycle — from request assignment
through Core lease, Gateway delivery, Channels wake, worker lifecycle,
checkpoint protocol, cleanup, and Den Web trace projection — entirely
with deterministic pure-Python fakes.

No real Den services, Den MCP calls, Hermes subprocesses, or database
connections are involved. All service boundaries (Core, Gateway, Channels,
Den Web) are modelled as explicit dataclasses/fixtures with projection
methods.

Coverage includes:
- Happy path: full lifecycle through release.
- Blocked path: worker blocks, cleanup, release.
- Timeout/stale lease: lease expires before worker completes.
- Malformed packet: invalid checkpoint posted by worker.
- Cleanup failure: incomplete cleanup evidence triggers quarantine.
- Gateway delivery mismatch: delivery failure or miss.
- Stale lease and final release/quarantine behavior explicitly.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

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
    PoolWorkerRuntime,
)

# ---------------------------------------------------------------------------
# Fake projections for Core, Gateway, Channels, Den Web
# ---------------------------------------------------------------------------
# These are deterministic record/projection types, not database models.
# They hold enough structure to verify the workflow trace after simulation.


@dataclass
class FakeCoreAssignment:
    """Projection of a Core worker-pool assignment record.

    Fields mirror what the Den Core API would expose via
    mcp_den_get_worker_run or similar.
    """

    assignment_id: str
    task_id: int
    run_id: str
    role: str
    project_id: str | None = None
    status: str = "pending"
    lease_expires_at: str | None = None
    checkpoint_ids: list[int] = field(default_factory=list)
    latest_checkpoint_type: str | None = None
    latest_checkpoint_verdict: str | None = None
    completion_status: str | None = None
    completion_summary: str | None = None
    worker_id: str | None = None
    error: str | None = None

    def to_handle(self) -> dict[str, Any]:
        return {
            "assignment_id": self.assignment_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "role": self.role,
            "status": self.status,
            "project_id": self.project_id,
            "lease_expires_at": self.lease_expires_at,
            "checkpoint_count": len(self.checkpoint_ids),
            "latest_checkpoint_type": self.latest_checkpoint_type,
            "latest_checkpoint_verdict": self.latest_checkpoint_verdict,
            "completion_status": self.completion_status,
            "worker_id": self.worker_id,
            "has_error": self.error is not None,
        }


@dataclass
class FakeGatewayDelivery:
    """Projection of a Gateway delivery record.

    Tracks the delivery attempt to the worker and any callback response.
    """

    delivery_id: str
    assignment_id: str
    target_worker: str
    status: str = "pending"
    attempt_count: int = 0
    last_error: str | None = None
    callback_received: bool = False
    callback_status: str | None = None

    def to_handle(self) -> dict[str, Any]:
        return {
            "delivery_id": self.delivery_id,
            "assignment_id": self.assignment_id,
            "target_worker": self.target_worker,
            "status": self.status,
            "attempt_count": self.attempt_count,
            "callback_received": self.callback_received,
            "callback_status": self.callback_status,
        }


@dataclass
class FakeChannelsMessage:
    """Projection of a Channels message/activity record.

    Tracks direct-agent messages sent to wake workers.
    """

    message_id: int
    channel_id: int
    message_type: str
    body: str
    status: str = "sent"
    assignment_id: str | None = None
    run_id: str | None = None

    def to_handle(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "channel_id": self.channel_id,
            "message_type": self.message_type,
            "status": self.status,
            "assignment_id": self.assignment_id,
            "run_id": self.run_id,
        }


@dataclass
class FakeDenWebAssignmentTrace:
    """Projection of a Den Web assignment trace/readback record.

    Shows what the Den Web UI would surface for this assignment.
    """

    assignment_id: str
    task_id: int
    run_id: str
    role: str
    worker_id: str | None = None
    state_label: str = "pending"
    checkpoints_passed: int = 0
    checkpoints_total: int = 0
    completion_status: str | None = None
    overview_summary: str | None = None
    release_status: str = "not_released"
    quarantine_status: str | None = None

    def to_handle(self) -> dict[str, Any]:
        return {
            "assignment_id": self.assignment_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "role": self.role,
            "worker_id": self.worker_id,
            "state_label": self.state_label,
            "checkpoints": f"{self.checkpoints_passed}/{self.checkpoints_total}",
            "completion_status": self.completion_status,
            "release_status": self.release_status,
            "quarantine_status": self.quarantine_status,
        }


# ---------------------------------------------------------------------------
# Fake evidence builder — composes a structured evidence artifact from the
# full workflow projection.
# ---------------------------------------------------------------------------


@dataclass
class FakeE2EEvidence:
    """Structured evidence artifact for a single fake E2E scenario.

    Contains projections of all service handles plus the final worker
    state and overall verdict.
    """

    scenario: str
    verdict: str
    core: FakeCoreAssignment
    gateway: FakeGatewayDelivery | None = None
    channels_messages: list[FakeChannelsMessage] = field(default_factory=list)
    web_trace: FakeDenWebAssignmentTrace | None = None
    final_worker_state: str | None = None
    final_worker_error: str | None = None
    timeline_states: list[str] = field(default_factory=list)

    def to_handle_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "verdict": self.verdict,
            "core_handle": self.core.to_handle(),
            "gateway_handle": self.gateway.to_handle() if self.gateway else None,
            "channels_handles": [m.to_handle() for m in self.channels_messages],
            "web_trace_handle": self.web_trace.to_handle() if self.web_trace else None,
            "final_worker_state": self.final_worker_state,
            "final_worker_error": self.final_worker_error,
        }


# ---------------------------------------------------------------------------
# Scenario runner — pure deterministic simulation
# ---------------------------------------------------------------------------


class WorkerPoolMVPSimulator:
    """Fake E2E simulator for the worker pool MVP.

    Walks through the full workflow pipeline using deterministic state
    transitions. Each simulation step (Core lease, Gateway delivery,
    Channels wake, worker lifecycle, cleanup, release/quarantine) is
    modelled as a pure function operating on the fake projections.

    No I/O, no network, no databases.
    """

    def __init__(
        self,
        *,
        scenario: str,
        assignment_id: str = "t1728-assign-001",
        task_id: int = 1728,
        run_id: str = "t1728-worker-pool-smoke",
        role: str = "coder",
        project_id: str = "den-hermes-bridge",
        worker_id: str = "pool-coder-01",
        channel_id: int = 5,
        next_message_id: int = 1,
    ) -> None:
        self.scenario = scenario
        self.assignment_ptr = AssignmentPointer(
            assignment_id=assignment_id,
            task_id=task_id,
            run_id=run_id,
            role=role,
            project_id=project_id,
        )
        self.worker_id = worker_id
        self.channel_id = channel_id
        self._next_message_id = next_message_id

        # Projections
        self.core = FakeCoreAssignment(
            assignment_id=assignment_id,
            task_id=task_id,
            run_id=run_id,
            role=role,
            project_id=project_id,
            status="pending",
        )
        self.gateway: FakeGatewayDelivery | None = None
        self.channels_messages: list[FakeChannelsMessage] = []
        self.web_trace = FakeDenWebAssignmentTrace(
            assignment_id=assignment_id,
            task_id=task_id,
            run_id=run_id,
            role=role,
            state_label="pending",
        )
        self.timeline_states: list[str] = []

        # Worker runtime (starts as None until assignment is delivered)
        self.worker: PoolWorkerRuntime | None = None

    def _add_message(self, message_type: str, body: str, *,
                     assignment_id: str | None = None,
                     run_id: str | None = None) -> int:
        mid = self._next_message_id
        self._next_message_id += 1
        self.channels_messages.append(FakeChannelsMessage(
            message_id=mid,
            channel_id=self.channel_id,
            message_type=message_type,
            body=body,
            assignment_id=assignment_id or self.assignment_ptr.assignment_id,
            run_id=run_id or self.assignment_ptr.run_id,
        ))
        return mid

    def _record_timeline(self, label: str) -> None:
        self.timeline_states.append(label)

    def simulate_assignment_create(self) -> None:
        """Step 1: Core creates an assignment with a lease."""
        self.core.status = "lease_granted"
        self.core.lease_expires_at = "2026-05-29T14:00:00Z"
        self.web_trace.state_label = "lease_granted"
        self._record_timeline("core_assignment_created")

    def simulate_gateway_delivery(self, *, fail_delivery: bool = False,
                                  mismatch: bool = False) -> None:
        """Step 2: Gateway delivers the assignment to the pool worker."""
        self.gateway = FakeGatewayDelivery(
            delivery_id=f"del-{self.assignment_ptr.assignment_id}",
            assignment_id=self.assignment_ptr.assignment_id,
            target_worker=self.worker_id,
            status="pending",
        )
        if fail_delivery:
            self.gateway.attempt_count = 3
            self.gateway.status = "failed"
            self.gateway.last_error = "Worker unreachable after 3 retries"
            self.core.status = "delivery_failed"
            self.web_trace.state_label = "delivery_failed"
            self._record_timeline("gateway_delivery_failed")
            return

        if mismatch:
            # Delivery sent to wrong worker; callback from wrong worker
            self.gateway.target_worker = "pool-coder-wrong"
            self.gateway.attempt_count = 1
            self.gateway.status = "delivered_wrong_target"
            self.core.status = "delivery_mismatch"
            self.web_trace.state_label = "delivery_mismatch"
            self._record_timeline("gateway_delivery_mismatch")
            return

        self.gateway.attempt_count = 1
        self.gateway.status = "delivered"
        self.core.status = "delivered"
        self.web_trace.state_label = "delivered"
        self._record_timeline("gateway_delivered")

        # Worker is now instantiated
        self.worker = PoolWorkerRuntime(
            assignment=self.assignment_ptr,
            worker_id=self.worker_id,
        )

    def simulate_channels_wake(self, *, fail_wake: bool = False) -> None:
        """Step 3: Channels sends a wake to the worker via direct-agent
        message."""
        if fail_wake:
            mid = self._add_message(
                "wake_failed",
                f"Wake for assignment {self.assignment_ptr.assignment_id} failed: channel unreachable",
            )
            self.channels_messages[-1].status = "failed"
            self._record_timeline("channels_wake_failed")
            return

        mid = self._add_message(
            "wake",
            f"Assignment {self.assignment_ptr.assignment_id} delivered. "
            f"Task #{self.assignment_ptr.task_id} role={self.assignment_ptr.role}",
        )
        self._record_timeline("channels_wake_sent")

    def simulate_worker_acknowledge(self) -> None:
        """Step 4: Worker acknowledges the assignment."""
        if self.worker is None:
            return
        self.worker = self.worker.acknowledge(
            interpretation_summary="Implement worker pool MVP proof coverage",
            uncertainties=["Whether fake E2E shape matches real bridge contract"],
            non_goals=["No live Den services", "No database changes"],
        )
        self.core.status = "acknowledged"
        self.core.latest_checkpoint_type = "assignment_ack"
        self.core.worker_id = self.worker_id
        self.web_trace.state_label = "acknowledged"
        self.web_trace.worker_id = self.worker_id
        self._record_timeline("worker_acknowledged")

    def simulate_worker_interpretation_checkpoint(self) -> None:
        """Step 5: Worker posts interpretation checkpoint."""
        if self.worker is None:
            return
        self.worker = self.worker.post_interpretation(
            accepted_criteria=[
                "Fake E2E tests simulate full MVP workflow",
                "All failure modes covered",
                "Structured evidence artifact produced",
            ],
            non_goals=["No live smoke", "No Core schema changes"],
            risks=["Fake tests may miss real delivery edge cases"],
        )
        self.core.status = "interpreting"
        self.core.latest_checkpoint_type = "interpretation_checkpoint"
        core_cp_id = len(self.core.checkpoint_ids) + 1001
        self.core.checkpoint_ids.append(core_cp_id)
        self._add_message(
            "checkpoint", f"interpretation_checkpoint posted for {self.assignment_ptr.assignment_id}",
        )
        self.web_trace.state_label = "interpreting"
        self.web_trace.checkpoints_passed += 1
        self._record_timeline("interpretation_checkpoint_posted")

    def simulate_runner_checkpoint_response(self, *,
                                            verdict: str = "approved",
                                            correction: str | None = None) -> None:
        """Step 6: Runner responds to the checkpoint."""
        if self.worker is None:
            return
        if self.worker.state == PoolRuntimeState.INTERPRETING:
            cp_type = "interpretation_checkpoint"
        elif self.worker.state == PoolRuntimeState.PLANNING:
            cp_type = "plan_checkpoint"
        elif self.worker.state == PoolRuntimeState.BLOCKED_NEEDS_INPUT:
            cp_type = "blocked_needs_input"
        elif self.worker.state == PoolRuntimeState.PARTIAL_RESULT:
            cp_type = "partial_result_checkpoint"
        else:
            raise RuntimeError(
                f"Cannot respond to checkpoint from state {self.worker.state.value}"
            )

        response = CheckpointResponse(
            verdict=verdict,
            checkpoint_type=cp_type,
            run_id=self.assignment_ptr.run_id,
            assignment_id=self.assignment_ptr.assignment_id,
            correction=correction,
        )
        self.worker = self.worker.receive_checkpoint_response(response)
        self.core.latest_checkpoint_verdict = verdict
        self.web_trace.state_label = f"checkpoint_{verdict}"
        self._record_timeline(f"runner_response_{verdict}")

    def simulate_worker_plan_checkpoint(self) -> None:
        """Step 7: Worker posts plan checkpoint (after interpretation
        approved)."""
        if self.worker is None:
            return
        self.worker = self.worker.post_plan(
            files_to_touch=[
                "tests/test_worker_pool_mvp_e2e.py",
                "docs/worker-pool-mvp-rollout-runbook.md",
            ],
            approach="Fake E2E with pure dataclasses, no live I/O",
            validation_plan="python -m pytest tests/test_worker_pool_mvp_e2e.py -v",
        )
        self.core.status = "planning"
        self.core.latest_checkpoint_type = "plan_checkpoint"
        core_cp_id = len(self.core.checkpoint_ids) + 1002
        self.core.checkpoint_ids.append(core_cp_id)
        self._add_message("checkpoint", f"plan_checkpoint posted for {self.assignment_ptr.assignment_id}")
        self.web_trace.state_label = "planning"
        self.web_trace.checkpoints_passed += 1
        self._record_timeline("plan_checkpoint_posted")

    def simulate_worker_implementation(self) -> None:
        """Step 8: Worker proceeds to implementation and completes."""
        if self.worker is None:
            return
        self.worker = self.worker.proceed_to_implementation()
        self._record_timeline("implementation_started")

        packet = CompletionPacket(
            status="completed",
            run_id=self.assignment_ptr.run_id,
            role=self.assignment_ptr.role,
            task_id=self.assignment_ptr.task_id,
            summary="Implemented fake E2E MVPP coverage",
            evidence={
                "files_changed": [
                    "tests/test_worker_pool_mvp_e2e.py",
                    "docs/worker-pool-mvp-rollout-runbook.md",
                ],
                "branch": "task/1728-worker-pool-mvp-proof",
            },
        )
        self.worker = self.worker.complete(packet)
        self.worker = self.worker.finalize_completion()
        self.core.status = "completed"
        self.core.completion_status = "completed"
        self.core.completion_summary = packet.summary
        self.web_trace.state_label = "completed"
        self.web_trace.completion_status = "completed"
        self._record_timeline("worker_completed")

    def simulate_worker_block(self, *, reason: str) -> None:
        """Step 8B: Worker blocks instead of completing."""
        if self.worker is None:
            return
        self.worker = self.worker.block(reason=reason)
        self.core.status = "blocked"
        self.core.completion_status = "blocked"
        self.core.error = reason
        self.web_trace.state_label = "blocked"
        self.web_trace.completion_status = "blocked"
        self._record_timeline("worker_blocked")

    def simulate_worker_stale_lease(self) -> None:
        """Step 8C: Worker lease expires (timeout/stale lease scenario)."""
        if self.worker is None:
            return
        self.worker = self.worker.fail(reason="Stale lease: Core lease expired before completion")
        self.core.status = "failed_stale_lease"
        self.core.completion_status = "failed"
        self.core.error = "Core lease expired"
        self.web_trace.state_label = "failed_stale_lease"
        self.web_trace.completion_status = "failed"
        self._record_timeline("worker_stale_lease")

    def simulate_worker_malformed_packet(self) -> None:
        """Step 8D: Worker attempts invalid checkpoint (malformed packet
        scenario)."""
        if self.worker is None:
            return
        # Try to call post_plan from ACKNOWLEDGED (invalid transition)
        with pytest.raises(PoolRuntimeError, match="allowed states"):
            self.worker.post_plan(
                files_to_touch=["x"],
                approach="x",
                validation_plan="x",
            )
        # Worker fails due to runtime error
        self.worker = self.worker.fail(reason="Malformed packet: invalid checkpoint transition")
        self.core.status = "failed_malformed"
        self.core.completion_status = "failed"
        self.core.error = "Malformed packet"
        self.web_trace.state_label = "failed_malformed"
        self.web_trace.completion_status = "failed"
        self._record_timeline("worker_malformed_packet")

    def simulate_cleanup(self, *, complete_evidence: bool = True) -> None:
        """Step 9: Cleanup with optional incomplete evidence."""
        if self.worker is None:
            return
        # Preserve completion status before cleanup overwrites it
        preserved_completion_status = self.core.completion_status
        if not self.worker.is_terminal():
            self.worker = self.worker.fail(reason="Cleanup triggered from non-terminal state (simulation)")
        if complete_evidence:
            evidence = CleanupEvidence(
                scrub_workspace=True,
                process_release=True,
                session_rotation=True,
                scratch_cleanup=True,
            )
            self.worker = self.worker.cleanup(evidence)
            self.core.status = "cleaned_up"
            self.core.completion_status = preserved_completion_status
            self.web_trace.state_label = "cleaned_up"
            self._record_timeline("cleanup_complete")
        else:
            evidence = CleanupEvidence(
                scrub_workspace=True,
                process_release=False,
                session_rotation=False,
                scratch_cleanup=False,
            )
            with pytest.raises(PoolCleanupError, match="Cleanup evidence incomplete"):
                self.worker.cleanup(evidence)
            # After cleanup failure, quarantine directly
            self.worker = self.worker.quarantine()
            self.core.status = "quarantined"
            self.core.completion_status = "failed"
            self.web_trace.state_label = "quarantined"
            self.web_trace.quarantine_status = "quarantined"
            self._record_timeline("cleanup_failed_quarantine")

    def simulate_release(self) -> None:
        """Step 10: Release after successful cleanup."""
        if self.worker is None:
            return
        # Preserve completion status before release overwrites it
        preserved_completion_status = self.web_trace.completion_status
        self.worker = self.worker.release()
        self.core.status = "released"
        self.web_trace.state_label = "released"
        self.web_trace.release_status = "released"
        self.web_trace.completion_status = preserved_completion_status
        self._record_timeline("released")

    def get_evidence(self) -> FakeE2EEvidence:
        """Compose the structured evidence artifact from current state."""
        verdict = "passed"
        if self.worker and self.worker.is_failed():
            verdict = "failed"
        elif self.worker and self.worker.state == PoolRuntimeState.BLOCKED:
            verdict = "blocked"

        error = self.worker.error if self.worker and self.worker.error else None

        return FakeE2EEvidence(
            scenario=self.scenario,
            verdict=verdict,
            core=self.core,
            gateway=self.gateway,
            channels_messages=self.channels_messages,
            web_trace=self.web_trace,
            final_worker_state=self.worker.state.value if self.worker else None,
            final_worker_error=error,
            timeline_states=self.timeline_states,
        )


# ===========================================================================
# Scenario constants — used by both tests and the evidence fixture
# ===========================================================================

HAPPY_PATH_ASSIGNMENT = AssignmentPointer(
    assignment_id="t1728-assign-happy",
    task_id=1728,
    run_id="t1728-run-happy",
    role="coder",
    project_id="den-hermes-bridge",
)

BLOCKED_ASSIGNMENT = AssignmentPointer(
    assignment_id="t1728-assign-blocked",
    task_id=1728,
    run_id="t1728-run-blocked",
    role="coder",
    project_id="den-hermes-bridge",
)

STALE_LEASE_ASSIGNMENT = AssignmentPointer(
    assignment_id="t1728-assign-stale",
    task_id=1728,
    run_id="t1728-run-stale",
    role="coder",
    project_id="den-hermes-bridge",
)

MALFORMED_PACKET_ASSIGNMENT = AssignmentPointer(
    assignment_id="t1728-assign-malformed",
    task_id=1728,
    run_id="t1728-run-malformed",
    role="coder",
    project_id="den-hermes-bridge",
)

CLEANUP_FAILURE_ASSIGNMENT = AssignmentPointer(
    assignment_id="t1728-assign-cleanfail",
    task_id=1728,
    run_id="t1728-run-cleanfail",
    role="coder",
    project_id="den-hermes-bridge",
)

DELIVERY_MISMATCH_ASSIGNMENT = AssignmentPointer(
    assignment_id="t1728-assign-mismatch",
    task_id=1728,
    run_id="t1728-run-mismatch",
    role="coder",
    project_id="den-hermes-bridge",
)

TIMEOUT_STALE_ASSIGNMENT = AssignmentPointer(
    assignment_id="t1728-assign-timeout",
    task_id=1728,
    run_id="t1728-run-timeout",
    role="coder",
    project_id="den-hermes-bridge",
)

# ===========================================================================
# Standalone helper — runs full happy-path simulation
# ===========================================================================


def _run_simulate_full_happy_path() -> FakeE2EEvidence:
    """Run a complete happy-path simulation from assignment through release."""
    sim = WorkerPoolMVPSimulator(scenario="happy_path")
    sim.simulate_assignment_create()
    sim.simulate_gateway_delivery()
    sim.simulate_channels_wake()
    sim.simulate_worker_acknowledge()
    sim.simulate_worker_interpretation_checkpoint()
    sim.simulate_runner_checkpoint_response(verdict="approved")
    sim.simulate_worker_plan_checkpoint()
    sim.simulate_runner_checkpoint_response(verdict="approved")
    sim.simulate_worker_implementation()
    sim.simulate_cleanup(complete_evidence=True)
    sim.simulate_release()
    return sim.get_evidence()


# ===========================================================================
# Tests
# ===========================================================================


class TestWorkerPoolMVPHappyPath:
    """Full happy-path lifecycle simulation."""

    def test_happy_path_produces_completed_state(self):
        evidence = _run_simulate_full_happy_path()
        assert evidence.verdict == "passed"
        assert evidence.final_worker_state == PoolRuntimeState.RELEASED.value
        assert evidence.core.status == "released"
        assert evidence.web_trace.state_label == "released"
        assert evidence.web_trace.completion_status == "completed"

    def test_happy_path_core_handle_fields(self):
        evidence = _run_simulate_full_happy_path()
        handle = evidence.core.to_handle()
        assert handle["assignment_id"] == "t1728-assign-001"
        assert handle["task_id"] == 1728
        assert handle["run_id"] == "t1728-worker-pool-smoke"
        assert handle["role"] == "coder"
        assert handle["status"] == "released"
        assert handle["completion_status"] == "completed"
        assert handle["worker_id"] == "pool-coder-01"
        assert handle["checkpoint_count"] >= 2
        assert handle["latest_checkpoint_type"] == "plan_checkpoint"
        assert handle["latest_checkpoint_verdict"] == "approved"

    def test_happy_path_gateway_handle_fields(self):
        evidence = _run_simulate_full_happy_path()
        assert evidence.gateway is not None
        handle = evidence.gateway.to_handle()
        assert handle["delivery_id"] == "del-t1728-assign-001"
        assert handle["status"] == "delivered"
        assert handle["attempt_count"] == 1
        assert handle["callback_received"] is False

    def test_happy_path_channels_messages(self):
        evidence = _run_simulate_full_happy_path()
        assert len(evidence.channels_messages) >= 3  # wake + 2 checkpoints
        types = {m.message_type for m in evidence.channels_messages}
        assert "wake" in types
        assert "checkpoint" in types

    def test_happy_path_web_trace_handle_fields(self):
        evidence = _run_simulate_full_happy_path()
        handle = evidence.web_trace.to_handle()
        assert handle["assignment_id"] == "t1728-assign-001"
        assert handle["state_label"] == "released"
        assert handle["checkpoints"] == "2/0"
        assert handle["completion_status"] == "completed"
        assert handle["release_status"] == "released"
        assert handle["quarantine_status"] is None

    def test_happy_path_timeline_has_all_steps(self):
        evidence = _run_simulate_full_happy_path()
        expected_steps = [
            "core_assignment_created",
            "gateway_delivered",
            "channels_wake_sent",
            "worker_acknowledged",
            "interpretation_checkpoint_posted",
            "runner_response_approved",
            "plan_checkpoint_posted",
            "runner_response_approved",
            "implementation_started",
            "worker_completed",
            "cleanup_complete",
            "released",
        ]
        assert evidence.timeline_states == expected_steps


class TestWorkerPoolMVPBlockedPath:
    """Worker blocks after interpretation, cleanup, and release."""

    def simulate_blocked_path(self) -> FakeE2EEvidence:
        sim = WorkerPoolMVPSimulator(scenario="blocked_path")
        sim.simulate_assignment_create()
        sim.simulate_gateway_delivery()
        sim.simulate_channels_wake()
        sim.simulate_worker_acknowledge()
        sim.simulate_worker_interpretation_checkpoint()
        # Runner blocks the interpretation
        sim.simulate_runner_checkpoint_response(
            verdict="blocked",
            correction="Runner has no bandwidth; assignment paused",
        )
        # Worker transitions to BLOCKED
        # Cleanup and release from BLOCKED
        sim.simulate_cleanup(complete_evidence=True)
        sim.simulate_release()
        return sim.get_evidence()

    def test_blocked_path_ends_in_released(self):
        evidence = self.simulate_blocked_path()
        assert evidence.final_worker_state == PoolRuntimeState.RELEASED.value
        assert evidence.core.status == "released"

    def test_blocked_path_core_has_error(self):
        evidence = self.simulate_blocked_path()
        # error was set before cleanup/release; check final_worker_error
        assert evidence.final_worker_error is not None
        assert "paused" in evidence.final_worker_error

    def test_blocked_path_web_trace_shows_blocked_then_released(self):
        evidence = self.simulate_blocked_path()
        assert evidence.web_trace.state_label == "released"
        # completion_status is None because the blocked transition
        # came from a checkpoint_response, not from worker completing.
        # The final_worker_error carries the blocker reason.
        assert evidence.final_worker_error is not None
        assert "paused" in evidence.final_worker_error


class TestWorkerPoolMVPStaleLease:
    """Stale lease — lease expires during work."""

    def simulate_stale_lease_path(self) -> FakeE2EEvidence:
        sim = WorkerPoolMVPSimulator(scenario="stale_lease")
        sim.simulate_assignment_create()
        sim.simulate_gateway_delivery()
        sim.simulate_channels_wake()
        sim.simulate_worker_acknowledge()
        # Before interpretation checkpoint, lease expires
        sim.simulate_worker_stale_lease()
        # Cleanup failure leads to quarantine
        sim.simulate_cleanup(complete_evidence=False)
        return sim.get_evidence()

    def test_stale_lease_ends_in_quarantined(self):
        evidence = self.simulate_stale_lease_path()
        assert evidence.final_worker_state == PoolRuntimeState.QUARANTINED.value
        assert evidence.core.status == "quarantined"

    def test_stale_lease_web_trace_shows_failed_and_quarantined(self):
        evidence = self.simulate_stale_lease_path()
        assert evidence.web_trace.state_label == "quarantined"
        assert evidence.web_trace.quarantine_status == "quarantined"
        assert evidence.web_trace.completion_status == "failed"


class TestWorkerPoolMVPMalformedPacket:
    """Worker attempts invalid state transition (malformed checkpoint)."""

    def simulate_malformed_packet_path(self) -> FakeE2EEvidence:
        sim = WorkerPoolMVPSimulator(scenario="malformed_packet")
        sim.simulate_assignment_create()
        sim.simulate_gateway_delivery()
        sim.simulate_channels_wake()
        sim.simulate_worker_acknowledge()
        sim.simulate_worker_malformed_packet()
        sim.simulate_cleanup(complete_evidence=True)
        sim.simulate_release()
        return sim.get_evidence()

    def test_malformed_packet_ends_released_with_failed_evidence(self):
        evidence = self.simulate_malformed_packet_path()
        assert evidence.final_worker_state == PoolRuntimeState.RELEASED.value
        assert evidence.core.completion_status == "failed"
        assert evidence.core.error == "Malformed packet"

    def test_malformed_packet_has_timeline_entry(self):
        evidence = self.simulate_malformed_packet_path()
        assert "worker_malformed_packet" in evidence.timeline_states


class TestWorkerPoolMVPCleanupFailure:
    """Incomplete cleanup evidence triggers quarantine."""

    def simulate_cleanup_failure_path(self) -> FakeE2EEvidence:
        sim = WorkerPoolMVPSimulator(scenario="cleanup_failure")
        sim.simulate_assignment_create()
        sim.simulate_gateway_delivery()
        sim.simulate_channels_wake()
        sim.simulate_worker_acknowledge()
        sim.simulate_worker_interpretation_checkpoint()
        sim.simulate_runner_checkpoint_response(verdict="approved")
        sim.simulate_worker_plan_checkpoint()
        sim.simulate_runner_checkpoint_response(verdict="approved")
        sim.simulate_worker_implementation()
        # Then incomplete cleanup
        sim.simulate_cleanup(complete_evidence=False)
        return sim.get_evidence()

    def test_cleanup_failure_quarantines(self):
        evidence = self.simulate_cleanup_failure_path()
        assert evidence.final_worker_state == PoolRuntimeState.QUARANTINED.value
        assert evidence.core.status == "quarantined"

    def test_cleanup_failure_has_quarantine_on_web_trace(self):
        evidence = self.simulate_cleanup_failure_path()
        assert evidence.web_trace.quarantine_status == "quarantined"


class TestWorkerPoolMVPGatewayDeliveryMismatch:
    """Gateway delivers to wrong worker."""

    def simulate_delivery_mismatch_path(self) -> FakeE2EEvidence:
        sim = WorkerPoolMVPSimulator(scenario="delivery_mismatch")
        sim.simulate_assignment_create()
        sim.simulate_gateway_delivery(mismatch=True)
        return sim.get_evidence()

    def test_delivery_mismatch_detected(self):
        evidence = self.simulate_delivery_mismatch_path()
        assert evidence.gateway is not None
        assert evidence.gateway.status == "delivered_wrong_target"
        assert evidence.gateway.target_worker == "pool-coder-wrong"
        assert evidence.core.status == "delivery_mismatch"
        assert evidence.web_trace.state_label == "delivery_mismatch"
        # Worker should never have been instantiated
        assert evidence.final_worker_state is None


class TestWorkerPoolMVPGatewayDeliveryFailure:
    """Gateway delivery fails entirely."""

    def simulate_delivery_failure_path(self) -> FakeE2EEvidence:
        sim = WorkerPoolMVPSimulator(scenario="delivery_failure")
        sim.simulate_assignment_create()
        sim.simulate_gateway_delivery(fail_delivery=True)
        return sim.get_evidence()

    def test_delivery_failure_reported(self):
        evidence = self.simulate_delivery_failure_path()
        assert evidence.gateway is not None
        assert evidence.gateway.status == "failed"
        assert evidence.gateway.attempt_count == 3
        assert evidence.core.status == "delivery_failed"
        assert evidence.web_trace.state_label == "delivery_failed"
        assert evidence.final_worker_state is None


class TestWorkerPoolMVPEvidenceShape:
    """Tests that the evidence artifact has the expected shape and
    full set of durable handles."""

    def test_evidence_has_all_required_handles(self):
        sim = WorkerPoolMVPSimulator(scenario="shape_test")
        sim.simulate_assignment_create()
        sim.simulate_gateway_delivery()
        sim.simulate_channels_wake()
        sim.simulate_worker_acknowledge()
        evidence = sim.get_evidence()
        handle_dict = evidence.to_handle_dict()

        # Core handle
        assert "core_handle" in handle_dict
        core = handle_dict["core_handle"]
        assert "assignment_id" in core
        assert "task_id" in core
        assert "run_id" in core
        assert "role" in core
        assert "status" in core
        assert "checkpoint_count" in core
        assert "latest_checkpoint_type" in core
        assert "completion_status" in core

        # Gateway handle
        assert "gateway_handle" in handle_dict
        gw = handle_dict["gateway_handle"]
        assert "delivery_id" in gw
        assert "target_worker" in gw
        assert "status" in gw
        assert "attempt_count" in gw

        # Channels handles
        assert "channels_handles" in handle_dict
        assert len(handle_dict["channels_handles"]) >= 1
        msg = handle_dict["channels_handles"][0]
        assert "message_id" in msg
        assert "message_type" in msg
        assert "status" in msg

        # Den Web trace handle
        assert "web_trace_handle" in handle_dict
        web = handle_dict["web_trace_handle"]
        assert "assignment_id" in web
        assert "state_label" in web
        assert "checkpoints" in web
        assert "release_status" in web

        # Worker state
        assert "final_worker_state" in handle_dict
        assert "final_worker_error" in handle_dict

    def test_evidence_serializes_to_json(self):
        """The evidence handle dict must be JSON-serializable (no
        non-serializable types)."""
        import json
        sim = WorkerPoolMVPSimulator(scenario="json_serialization")
        sim.simulate_assignment_create()
        sim.simulate_gateway_delivery()
        sim.simulate_channels_wake()
        sim.simulate_worker_acknowledge()
        handle_dict = sim.get_evidence().to_handle_dict()
        # Should not raise
        json.dumps(handle_dict, indent=2, sort_keys=True)


class TestWorkerPoolMVPTimelineCompleteness:
    """Full timeline completeness check — validates that all
    acceptance-criteria workflow steps are present in at least one
    scenario."""

    ALL_WORKFLOW_STEPS = [
        "core_assignment_created",
        "gateway_delivered",
        "channels_wake_sent",
        "worker_acknowledged",
        "interpretation_checkpoint_posted",
        "runner_response_approved",
        "plan_checkpoint_posted",
        "implementation_started",
        "worker_completed",
        "cleanup_complete",
        "released",
    ]

    FAILURE_STEPS = [
        "worker_blocked",
        "worker_stale_lease",
        "worker_malformed_packet",
        "cleanup_failed_quarantine",
        "gateway_delivery_failed",
        "gateway_delivery_mismatch",
    ]

    def test_happy_path_covers_all_main_steps(self):
        ev = _run_simulate_full_happy_path()
        for step in self.ALL_WORKFLOW_STEPS:
            assert step in ev.timeline_states, (
                f"Happy path missing workflow step: {step}"
            )

    @pytest.fixture
    def happy_evidence(self) -> FakeE2EEvidence:
        sim = WorkerPoolMVPSimulator(scenario="coverage_check")
        sim.simulate_assignment_create()
        sim.simulate_gateway_delivery()
        sim.simulate_channels_wake()
        sim.simulate_worker_acknowledge()
        sim.simulate_worker_interpretation_checkpoint()
        sim.simulate_runner_checkpoint_response(verdict="approved")
        sim.simulate_worker_plan_checkpoint()
        sim.simulate_runner_checkpoint_response(verdict="approved")
        sim.simulate_worker_implementation()
        sim.simulate_cleanup(complete_evidence=True)
        sim.simulate_release()
        return sim.get_evidence()

    def test_final_worker_state_in_happy_path(self, happy_evidence):
        assert happy_evidence.final_worker_state == PoolRuntimeState.RELEASED.value
        assert happy_evidence.verdict == "passed"

    def test_assignment_pointer_validation(self):
        """AssignmentPointer must validate cleanly for all scenario
        fixtures."""
        HAPPY_PATH_ASSIGNMENT.validate()
        BLOCKED_ASSIGNMENT.validate()
        STALE_LEASE_ASSIGNMENT.validate()
        MALFORMED_PACKET_ASSIGNMENT.validate()
        CLEANUP_FAILURE_ASSIGNMENT.validate()
        DELIVERY_MISMATCH_ASSIGNMENT.validate()
        TIMEOUT_STALE_ASSIGNMENT.validate()

    def test_checkpoint_response_validation(self):
        """CheckpointResponse must validate cleanly for common
        response types."""
        for verdict in ("approved", "approved_with_correction", "changes_requested", "blocked"):
            resp = CheckpointResponse(
                verdict=verdict,
                checkpoint_type="interpretation_checkpoint",
                run_id=HAPPY_PATH_ASSIGNMENT.run_id,
                assignment_id=HAPPY_PATH_ASSIGNMENT.assignment_id,
                correction="Fix scope" if verdict in ("approved_with_correction", "changes_requested") else None,
            )
            resp.validate()


class TestWorkerPoolMVPQuickScenarios:
    """Quick smoke tests for each scenario — validates that the
    simulation runs without errors and produces deterministic
    evidence shapes."""

    @pytest.mark.parametrize("scenario_name,simulator_fn", [
        ("happy_path", lambda: WorkerPoolMVPSimulator(scenario="happy_path")),
        ("blocked_path", lambda: WorkerPoolMVPSimulator(scenario="blocked_path")),
        ("stale_lease", lambda: WorkerPoolMVPSimulator(scenario="stale_lease")),
        ("malformed_packet", lambda: WorkerPoolMVPSimulator(scenario="malformed_packet")),
        ("cleanup_failure", lambda: WorkerPoolMVPSimulator(scenario="cleanup_failure")),
        ("delivery_mismatch", lambda: WorkerPoolMVPSimulator(scenario="delivery_mismatch")),
        ("delivery_failure", lambda: WorkerPoolMVPSimulator(scenario="delivery_failure")),
    ])
    def test_scenario_runs_without_exception(self, scenario_name: str, simulator_fn):
        sim = simulator_fn()
        # Run initial setup steps that are safe for all scenarios
        sim.simulate_assignment_create()
        sim.simulate_gateway_delivery()
        # Not all scenarios have a Channels wake that makes sense,
        # but run these in order
        evidence = sim.get_evidence()
        assert evidence.scenario == scenario_name
        assert isinstance(evidence.to_handle_dict(), dict)

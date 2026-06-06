"""Persistent Hermes pool-worker runtime and cleanup policy.

This module defines a fakeable state machine for pool workers that can:
- Accept a Core assignment pointer and acknowledge it.
- Post typed checkpoints (interpretation, plan, partial_result, blocked_needs_input).
- Wait for checkpoint_response gating (approved / corrections / changes_requested / blocked).
- Complete with structured packets.
- Block or fail with determinstic artifact evidence.
- Clean up assignment-scoped state and release or quarantine safely.

Design invariants:
- The state machine is deterministic and fakeable: no real I/O in constructor.
- Assignment identity validation is fail-closed on mismatch.
- Cleanup failure => quarantine, never success.
- Mismatched run identities => blocked/failed, never completed.
- Zero/manual-only long-term memory by default.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CANONICAL_CHECKPOINT_TYPES = frozenset({
    "assignment_ack",
    "interpretation_checkpoint",
    "plan_checkpoint",
    "checkpoint_response",
    "partial_result_checkpoint",
    "blocked_needs_input",
})

CANONICAL_CHECKPOINT_VERDICTS = frozenset({
    "approved",
    "approved_with_correction",
    "changes_requested",
    "blocked",
})

CANONICAL_WORKER_ROLES = frozenset({
    "coder",
    "reviewer",
    "validator",
    "drift_checker",
    "packet_auditor",
})

REQUIRED_CLEANUP_EVIDENCE_FIELDS = frozenset({
    "scrub_workspace",
    "process_release",
    "session_rotation",
    "scratch_cleanup",
})


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PoolRuntimeState(str, enum.Enum):
    """Deterministic states for a pool worker's runtime lifecycle.

    Transitions form a DAG; invalid transitions raise PoolRuntimeError.
    """

    PENDING = "pending"
    """Assignment received but not yet acknowledged."""

    ACKNOWLEDGED = "acknowledged"
    """assignment_ack sent. Worker may post interpretation_checkpoint."""

    INTERPRETING = "interpreting"
    """interpretation_checkpoint posted; awaiting checkpoint_response."""

    INTERPRETATION_APPROVED = "interpretation_approved"
    """Interpretation approved. Worker may post plan_checkpoint."""

    PLANNING = "planning"
    """plan_checkpoint posted; awaiting checkpoint_response."""

    PLAN_APPROVED = "plan_approved"
    """Plan approved. Worker may proceed to implementation."""

    IMPLEMENTING = "implementing"
    """Actively implementing. May post partial_result_checkpoint."""

    BLOCKED_NEEDS_INPUT = "blocked_needs_input"
    """blocked_needs_input posted; awaiting checkpoint_response."""

    PARTIAL_RESULT = "partial_result"
    """Partial result checkpoint posted; awaiting checkpoint_response."""

    PARTIAL_RESULT_APPROVED = "partial_result_approved"
    """Partial result approved. Worker may continue implementation."""

    COMPLETING = "completing"
    """Finishing work; preparing completion packet."""

    COMPLETED = "completed"
    """Successfully completed with structured packet."""

    BLOCKED = "blocked"
    """Blocked without active checkpoint (terminal non-failure)."""

    FAILED = "failed"
    """Failed (infrastructure, malformed, or rejected)."""

    CLEANING_UP = "cleaning_up"
    """Cleanup in progress."""

    CLEANED_UP = "cleaned_up"
    """Deterministic cleanup evidence emitted."""

    RELEASED = "released"
    """Released by Core; assignment complete."""

    QUARANTINED = "quarantined"
    """Cleanup failed or evidence missing; isolated."""

    # Terminal / absorb states for validation.
    @classmethod
    def terminal_states(cls) -> frozenset[PoolRuntimeState]:
        return frozenset({
            cls.COMPLETED,
            cls.BLOCKED,
            cls.FAILED,
            cls.CLEANED_UP,
            cls.RELEASED,
            cls.QUARANTINED,
        })

    @classmethod
    def failed_states(cls) -> frozenset[PoolRuntimeState]:
        return frozenset({cls.FAILED, cls.QUARANTINED})

    @classmethod
    def success_states(cls) -> frozenset[PoolRuntimeState]:
        return frozenset({cls.COMPLETED, cls.CLEANED_UP, cls.RELEASED})

    @classmethod
    def busy_leak_states(cls) -> frozenset[PoolRuntimeState]:
        """Terminal states where a pool member with no active assignment is a leak."""
        return frozenset({cls.COMPLETED, cls.BLOCKED, cls.FAILED, cls.QUARANTINED})


# ---------------------------------------------------------------------------
# Diagnostic taxonomy for packet-auditor / worker-pool operational failures
# ---------------------------------------------------------------------------

CANONICAL_FAILURE_CATEGORIES: dict[str, str] = {
    "membership_not_active": (
        "Worker's target channel membership is not active. Restore membership "
        "in the target lane or re-provision with active wake policy."
    ),
    "wake_route_404": (
        "Wake bridge route returned 404. The endpoint or delivery path may be "
        "missing, or the target channel/member is not in a wakeable state."
    ),
    "auth_unhealthy": (
        "Profile auth/provider health check failed. The worker's OAuth token or "
        "API key may be expired, or the model provider is unreachable."
    ),
    "post_terminal_pool_state_leak": (
        "Pool member is in a terminal state but has no active Core assignment. "
        "The member should be released back to available or quarantined."
    ),
}


@dataclass(frozen=True)
class PostTerminalBusyLeak:
    """Evidence that a pool member is stuck busy without an active assignment."""

    member_id: str
    state: str
    role: str | None = None
    assignment_id: str | None = None
    active_assignment_count: int = 0

    @property
    def category(self) -> str:
        return "post_terminal_pool_state_leak"

    def __str__(self) -> str:
        return (
            f"PostTerminalBusyLeak(member={self.member_id}, state={self.state}, "
            f"role={self.role}, assignment={self.assignment_id}, "
            f"active_assignments={self.active_assignment_count})"
        )


@dataclass(frozen=True)
class PoolMemberDiagnostic:
    """Structured diagnostic for a worker-pool operational failure.

    Categories are drawn from CANONICAL_FAILURE_CATEGORIES.
    """

    category: str
    member_id: str
    evidence: Mapping[str, Any] = field(default_factory=dict)
    recovery: str = ""
    severity: str = "critical"

    def __post_init__(self) -> None:
        if self.category not in CANONICAL_FAILURE_CATEGORIES:
            raise ValueError(
                f"Unknown failure category: {self.category!r}. "
                f"Known: {', '.join(sorted(CANONICAL_FAILURE_CATEGORIES))}"
            )

    @staticmethod
    def canonical_failure_categories() -> dict[str, str]:
        return dict(CANONICAL_FAILURE_CATEGORIES)

    def summary(self) -> str:
        desc = CANONICAL_FAILURE_CATEGORIES.get(self.category, self.category)
        parts = [
            f"[{self.category}] {self.member_id}",
            f"  Description: {desc}",
        ]
        if self.evidence:
            parts.append(f"  Evidence: {self.evidence}")
        if self.recovery:
            parts.append(f"  Recovery: {self.recovery}")
        return "\n".join(parts)

    def __str__(self) -> str:
        return self.summary()


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PoolRuntimeError(ValueError):
    """Raised for invalid state transitions, identity mismatches, or policy
    violations."""


class PoolCleanupError(RuntimeError):
    """Raised when cleanup evidence is missing or unsafe, triggering
    quarantine."""


# ---------------------------------------------------------------------------
# Core data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssignmentPointer:
    """Reference to a Core worker-pool assignment.

    This is what the pool worker receives via Channels/Gateway delivery.
    """
    assignment_id: str
    task_id: int
    run_id: str
    role: str
    project_id: str | None = None
    provider: str | None = None
    """Readback hint / expected-role annotation, not a runtime override source.

    Persistent pool workers use their deployed profile/gateway config as the
    runtime authority.  This field is descriptive only: it reflects what Core's
    pool metadata expected at provisioning time.  Assignment orchestrators must
    not feed this value as a ``--provider`` CLI flag to already-running pool
    gateways.
    """

    model: str | None = None
    """Readback hint / expected-role annotation, not a runtime override source.

    Same semantics as ``provider``.  Descriptive only; pool gateway config
    is the runtime authority.  Not used as a ``--model`` CLI override.
    """

    session_key: str | None = None
    environment_refs: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        errors: list[str] = []
        if not self.assignment_id or not self.assignment_id.strip():
            errors.append("assignment_id must not be empty")
        if not isinstance(self.task_id, int) or self.task_id <= 0:
            errors.append("task_id must be a positive integer")
        if not self.run_id or not self.run_id.strip():
            errors.append("run_id must not be empty")
        if self.role not in CANONICAL_WORKER_ROLES:
            canonical = ", ".join(sorted(CANONICAL_WORKER_ROLES))
            errors.append(f"role must be one of: {canonical}; got {self.role!r}")
        if errors:
            raise PoolRuntimeError(
                f"Invalid assignment pointer: {'; '.join(errors)}"
            )


@dataclass(frozen=True)
class CheckpointPayload:
    """A typed checkpoint posted by the worker (or response from runner)."""
    type: str
    assignment_id: str
    run_id: str
    role: str
    task_id: int
    content: Mapping[str, Any] = field(default_factory=dict)
    project_id: str | None = None

    def validate(self) -> None:
        if self.type not in CANONICAL_CHECKPOINT_TYPES:
            types = ", ".join(sorted(CANONICAL_CHECKPOINT_TYPES))
            raise PoolRuntimeError(
                f"Unknown checkpoint type {self.type!r}; expected one of: {types}"
            )


@dataclass(frozen=True)
class CheckpointResponse:
    """Runner response gate for a posted checkpoint."""
    verdict: str
    checkpoint_type: str
    run_id: str
    assignment_id: str
    correction: str | None = None
    notes: str | None = None

    def validate(self) -> None:
        if self.verdict not in CANONICAL_CHECKPOINT_VERDICTS:
            verdicts = ", ".join(sorted(CANONICAL_CHECKPOINT_VERDICTS))
            raise PoolRuntimeError(
                f"Unknown checkpoint verdict {self.verdict!r}; "
                f"expected one of: {verdicts}"
            )
        if self.verdict in ("approved_with_correction", "changes_requested"):
            if not self.correction:
                raise PoolRuntimeError(
                    f"Verdict {self.verdict!r} requires a non-empty correction"
                )
        if not self.run_id or not self.run_id.strip():
            raise PoolRuntimeError("checkpoint_response must have a non-empty run_id")
        if not self.assignment_id or not self.assignment_id.strip():
            raise PoolRuntimeError(
                "checkpoint_response must have a non-empty assignment_id"
            )


@dataclass(frozen=True)
class CompletionPacket:
    """Structured completion artifact for a pool worker run."""
    status: str
    run_id: str
    role: str
    task_id: int
    summary: str
    project_id: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None
    known_gaps: Sequence[str] | None = None

    def validate(self, *, expected_run_id: str, expected_role: str,
                 expected_task_id: int) -> None:
        errors: list[str] = []
        if self.run_id != expected_run_id:
            errors.append(
                f"run_id mismatch: expected {expected_run_id!r}, "
                f"got {self.run_id!r}"
            )
        if self.role != expected_role:
            errors.append(
                f"role mismatch: expected {expected_role!r}, got {self.role!r}"
            )
        if self.task_id != expected_task_id:
            errors.append(
                f"task_id mismatch: expected {expected_task_id}, "
                f"got {self.task_id}"
            )
        if not self.summary or not self.summary.strip():
            errors.append("summary must not be empty")
        if errors:
            raise PoolRuntimeError(
                f"Invalid completion packet: {'; '.join(errors)}"
            )


@dataclass(frozen=True)
class CleanupEvidence:
    """Deterministic evidence that assignment-scoped state has been
    cleaned up."""
    scrub_workspace: bool = False
    process_release: bool = False
    session_rotation: bool = False
    scratch_cleanup: bool = False
    notes: str | None = None

    def is_complete(self) -> bool:
        """All required cleanup actions must be confirmed."""
        return all(
            getattr(self, field, False) for field in REQUIRED_CLEANUP_EVIDENCE_FIELDS
        )

    def missing_fields(self) -> frozenset[str]:
        return frozenset(
            field for field in REQUIRED_CLEANUP_EVIDENCE_FIELDS
            if not getattr(self, field, False)
        )


# ---------------------------------------------------------------------------
# Core state machine
# ---------------------------------------------------------------------------


class PoolWorkerRuntime:
    """Deterministic state machine for a Hermes pool worker's lifecycle.

    This class is fakeable: all I/O (checkpoint posting, cleanup actions) is
    done through injected callables. The state machine itself is pure logic.

    Typical flow:
        runtime = PoolWorkerRuntime(assignment, worker_id)
        runtime = runtime.acknowledge()
        runtime = runtime.post_interpretation(...)
        runtime = runtime.receive_checkpoint_response(approved)
        runtime = runtime.post_plan(...)
        runtime = runtime.receive_checkpoint_response(approved)
        runtime = runtime.complete(...)
        runtime = runtime.cleanup(...)
        runtime = runtime.release()
    """

    def __init__(
        self,
        assignment: AssignmentPointer,
        worker_id: str,
        *,
        state: PoolRuntimeState | None = None,
        last_checkpoint: CheckpointPayload | None = None,
        last_response: CheckpointResponse | None = None,
        completion: CompletionPacket | None = None,
        cleanup_evidence: CleanupEvidence | None = None,
        error: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.assignment = assignment
        self.worker_id = worker_id
        self.state = state or PoolRuntimeState.PENDING
        self.last_checkpoint = last_checkpoint
        self.last_response = last_response
        self.completion = completion
        self.cleanup_evidence = cleanup_evidence
        self.error = error
        self.metadata = dict(metadata or {})

    # -----------------------------------------------------------------------
    # Identity validation helpers
    # -----------------------------------------------------------------------

    def validate_run_id_match(self, *, run_id: str) -> None:
        """Fail-closed: reject mismatched run identities."""
        if run_id != self.assignment.run_id:
            raise PoolRuntimeError(
                f"Run ID mismatch: expected {self.assignment.run_id!r}, "
                f"got {run_id!r}"
            )

    def validate_assignment_id_match(self, *, assignment_id: str) -> None:
        """Fail-closed: reject mismatched assignment identities."""
        if assignment_id != self.assignment.assignment_id:
            raise PoolRuntimeError(
                f"Assignment ID mismatch: expected "
                f"{self.assignment.assignment_id!r}, got {assignment_id!r}"
            )

    # -----------------------------------------------------------------------
    # Transition guard
    # -----------------------------------------------------------------------

    def _require_state(self, *allowed: PoolRuntimeState) -> None:
        if self.state not in allowed:
            allowed_names = ", ".join(s.value for s in allowed)
            raise PoolRuntimeError(
                f"Cannot transition from {self.state.value}; "
                f"allowed states: [{allowed_names}]"
            )

    def _transition(self, new_state: PoolRuntimeState) -> PoolWorkerRuntime:
        return PoolWorkerRuntime(
            assignment=self.assignment,
            worker_id=self.worker_id,
            state=new_state,
            last_checkpoint=self.last_checkpoint,
            last_response=self.last_response,
            completion=self.completion,
            cleanup_evidence=self.cleanup_evidence,
            error=self.error,
            metadata=self.metadata,
        )

    def _with_checkpoint(
        self,
        *,
        new_state: PoolRuntimeState,
        checkpoint_type: str,
        content: Mapping[str, Any],
    ) -> PoolWorkerRuntime:
        checkpoint = CheckpointPayload(
            type=checkpoint_type,
            assignment_id=self.assignment.assignment_id,
            run_id=self.assignment.run_id,
            role=self.assignment.role,
            task_id=self.assignment.task_id,
            content=content,
            project_id=self.assignment.project_id,
        )
        checkpoint.validate()
        return PoolWorkerRuntime(
            assignment=self.assignment,
            worker_id=self.worker_id,
            state=new_state,
            last_checkpoint=checkpoint,
            last_response=self.last_response,
            completion=self.completion,
            cleanup_evidence=self.cleanup_evidence,
            error=self.error,
            metadata=self.metadata,
        )

    # -----------------------------------------------------------------------
    # Lifecycle operations — each returns a new state (immutable transitions)
    # -----------------------------------------------------------------------

    def acknowledge(
        self,
        *,
        interpretation_summary: str,
        uncertainties: Sequence[str] | None = None,
        non_goals: Sequence[str] | None = None,
    ) -> PoolWorkerRuntime:
        """Acknowledge assignment and post assignment_ack.

        Moves from PENDING to ACKNOWLEDGED.
        """
        self._require_state(PoolRuntimeState.PENDING)
        if not interpretation_summary or not interpretation_summary.strip():
            raise PoolRuntimeError(
                "interpretation_summary is required for acknowledgment"
            )
        return self._with_checkpoint(
            new_state=PoolRuntimeState.ACKNOWLEDGED,
            checkpoint_type="assignment_ack",
            content={
                "interpretation_summary": interpretation_summary,
                "uncertainties": list(uncertainties or []),
                "non_goals": list(non_goals or []),
            },
        )

    def post_interpretation(
        self,
        *,
        accepted_criteria: Sequence[str],
        non_goals: Sequence[str],
        risks: Sequence[str] | None = None,
    ) -> PoolWorkerRuntime:
        """Post interpretation_checkpoint and enter INTERPRETING state.

        Moves from ACKNOWLEDGED to INTERPRETING.
        """
        self._require_state(PoolRuntimeState.ACKNOWLEDGED,
                            PoolRuntimeState.INTERPRETATION_APPROVED)
        if not accepted_criteria:
            raise PoolRuntimeError(
                "accepted_criteria must be a non-empty sequence"
            )
        if not non_goals:
            raise PoolRuntimeError("non_goals must be a non-empty sequence")
        return self._with_checkpoint(
            new_state=PoolRuntimeState.INTERPRETING,
            checkpoint_type="interpretation_checkpoint",
            content={
                "accepted_criteria": list(accepted_criteria),
                "non_goals": list(non_goals),
                "risks": list(risks or []),
            },
        )

    def post_plan(
        self,
        *,
        files_to_touch: Sequence[str],
        approach: str,
        validation_plan: str,
        risk_flags: Sequence[str] | None = None,
    ) -> PoolWorkerRuntime:
        """Post plan_checkpoint and enter PLANNING state.

        Moves from INTERPRETATION_APPROVED to PLANNING.
        """
        self._require_state(PoolRuntimeState.INTERPRETATION_APPROVED)
        if not files_to_touch:
            raise PoolRuntimeError(
                "files_to_touch must be a non-empty sequence"
            )
        if not approach or not approach.strip():
            raise PoolRuntimeError("approach is required for planning")
        if not validation_plan or not validation_plan.strip():
            raise PoolRuntimeError("validation_plan is required for planning")
        return self._with_checkpoint(
            new_state=PoolRuntimeState.PLANNING,
            checkpoint_type="plan_checkpoint",
            content={
                "files_to_touch": list(files_to_touch),
                "approach": approach,
                "validation_plan": validation_plan,
                "risk_flags": list(risk_flags or []),
            },
        )

    def proceed_to_implementation(self) -> PoolWorkerRuntime:
        """Move from PLAN_APPROVED to IMPLEMENTING."""
        self._require_state(PoolRuntimeState.PLAN_APPROVED)
        return self._transition(PoolRuntimeState.IMPLEMENTING)

    def post_partial_result(
        self,
        *,
        vertical_slice_paths: Sequence[str],
        status: str,
        diff_summary: str | None = None,
        open_issues: Sequence[str] | None = None,
    ) -> PoolWorkerRuntime:
        """Post partial_result_checkpoint during implementation.

        Moves from IMPLEMENTING to PARTIAL_RESULT.
        """
        self._require_state(PoolRuntimeState.IMPLEMENTING)
        if not vertical_slice_paths:
            raise PoolRuntimeError(
                "vertical_slice_paths must be a non-empty sequence"
            )
        if not status or not status.strip():
            raise PoolRuntimeError("status is required for partial result")
        return self._with_checkpoint(
            new_state=PoolRuntimeState.PARTIAL_RESULT,
            checkpoint_type="partial_result_checkpoint",
            content={
                "vertical_slice_paths": list(vertical_slice_paths),
                "status": status,
                "diff_summary": diff_summary,
                "open_issues": list(open_issues or []),
            },
        )

    def continue_implementation(self) -> PoolWorkerRuntime:
        """Resume implementation after partial result approved.

        Moves from PARTIAL_RESULT_APPROVED to IMPLEMENTING.
        """
        self._require_state(PoolRuntimeState.PARTIAL_RESULT_APPROVED)
        return self._transition(PoolRuntimeState.IMPLEMENTING)

    def post_blocked_needs_input(
        self,
        *,
        blocker_summary: str,
        blocker_category: str,
        recovery_guidance: str,
        evidence_handles: Sequence[str] | None = None,
    ) -> PoolWorkerRuntime:
        """Post blocked_needs_input checkpoint.

        This can be called from most non-terminal states.
        """
        self._require_state(
            PoolRuntimeState.PENDING,
            PoolRuntimeState.ACKNOWLEDGED,
            PoolRuntimeState.INTERPRETING,
            PoolRuntimeState.INTERPRETATION_APPROVED,
            PoolRuntimeState.PLANNING,
            PoolRuntimeState.PLAN_APPROVED,
            PoolRuntimeState.IMPLEMENTING,
            PoolRuntimeState.PARTIAL_RESULT,
            PoolRuntimeState.PARTIAL_RESULT_APPROVED,
            PoolRuntimeState.COMPLETING,
        )
        if not blocker_summary or not blocker_summary.strip():
            raise PoolRuntimeError(
                "blocker_summary is required for blocked_needs_input"
            )
        if not blocker_category or not blocker_category.strip():
            raise PoolRuntimeError(
                "blocker_category is required for blocked_needs_input"
            )
        if not recovery_guidance or not recovery_guidance.strip():
            raise PoolRuntimeError(
                "recovery_guidance is required for blocked_needs_input"
            )
        return self._with_checkpoint(
            new_state=PoolRuntimeState.BLOCKED_NEEDS_INPUT,
            checkpoint_type="blocked_needs_input",
            content={
                "blocker_summary": blocker_summary,
                "blocker_category": blocker_category,
                "recovery_guidance": recovery_guidance,
                "evidence_handles": list(evidence_handles or []),
            },
        )

    def receive_checkpoint_response(
        self, response: CheckpointResponse
    ) -> PoolWorkerRuntime:
        """Handle a runner checkpoint_response.

        The allowed source states and resulting states depend on the
        checkpoint_type encoded in the response.
        """
        response.validate()

        # Validate assignment and run identity match.
        self.validate_assignment_id_match(assignment_id=response.assignment_id)
        self.validate_run_id_match(run_id=response.run_id)

        verdict = response.verdict

        # Map response.checkpoint_type to allowed source states.
        type_state_map = {
            "interpretation_checkpoint": (PoolRuntimeState.INTERPRETING,),
            "plan_checkpoint": (PoolRuntimeState.PLANNING,),
            "partial_result_checkpoint": (PoolRuntimeState.PARTIAL_RESULT,),
            "blocked_needs_input": (PoolRuntimeState.BLOCKED_NEEDS_INPUT,),
        }

        allowed = type_state_map.get(response.checkpoint_type)
        if allowed is None:
            raise PoolRuntimeError(
                f"Cannot handle checkpoint_response for type "
                f"{response.checkpoint_type!r}; expected one of: "
                f"{', '.join(sorted(type_state_map))}"
            )

        self._require_state(*allowed)

        # Determine transition based on verdict.
        if verdict in ("approved", "approved_with_correction"):
            return self._resolve_approve(response)
        elif verdict == "changes_requested":
            return self._resolve_changes_requested(response)
        elif verdict == "blocked":
            return self._resolve_blocked(response)
        else:
            raise PoolRuntimeError(
                f"Unhandled verdict: {verdict!r}"
            )

    # -----------------------------------------------------------------------
    # Checkpoint response resolution helpers
    # -----------------------------------------------------------------------

    def _resolve_approve(
        self, response: CheckpointResponse
    ) -> PoolWorkerRuntime:
        cp_type = response.checkpoint_type
        target: PoolRuntimeState
        if cp_type == "interpretation_checkpoint":
            target = PoolRuntimeState.INTERPRETATION_APPROVED
        elif cp_type == "plan_checkpoint":
            target = PoolRuntimeState.PLAN_APPROVED
        elif cp_type == "partial_result_checkpoint":
            target = PoolRuntimeState.PARTIAL_RESULT_APPROVED
        elif cp_type == "blocked_needs_input":
            # Approved out of blocked_needs_input — go to ack for replan.
            target = PoolRuntimeState.ACKNOWLEDGED
        else:
            raise PoolRuntimeError(
                f"No approval mapping for checkpoint type: {cp_type!r}"
            )
        return PoolWorkerRuntime(
            assignment=self.assignment,
            worker_id=self.worker_id,
            state=target,
            last_checkpoint=self.last_checkpoint,
            last_response=response,
            completion=self.completion,
            cleanup_evidence=self.cleanup_evidence,
            error=self.error,
            metadata=self.metadata,
        )

    def _resolve_changes_requested(
        self, response: CheckpointResponse
    ) -> PoolWorkerRuntime:
        cp_type = response.checkpoint_type
        target: PoolRuntimeState
        if cp_type == "interpretation_checkpoint":
            # Go back to ACKNOWLEDGED to re-interpret.
            target = PoolRuntimeState.ACKNOWLEDGED
        elif cp_type == "plan_checkpoint":
            # Go back to INTERPRETATION_APPROVED to re-plan.
            target = PoolRuntimeState.INTERPRETATION_APPROVED
        elif cp_type == "partial_result_checkpoint":
            # Go back to IMPLEMENTING to revise.
            target = PoolRuntimeState.IMPLEMENTING
        elif cp_type == "blocked_needs_input":
            target = PoolRuntimeState.ACKNOWLEDGED
        else:
            raise PoolRuntimeError(
                f"No changes_requested mapping for checkpoint type: {cp_type!r}"
            )
        return PoolWorkerRuntime(
            assignment=self.assignment,
            worker_id=self.worker_id,
            state=target,
            last_checkpoint=self.last_checkpoint,
            last_response=response,
            completion=self.completion,
            cleanup_evidence=self.cleanup_evidence,
            error=self.error,
            metadata=self.metadata,
        )

    def _resolve_blocked(
        self, response: CheckpointResponse
    ) -> PoolWorkerRuntime:
        return PoolWorkerRuntime(
            assignment=self.assignment,
            worker_id=self.worker_id,
            state=PoolRuntimeState.BLOCKED,
            last_checkpoint=self.last_checkpoint,
            last_response=response,
            completion=self.completion,
            cleanup_evidence=self.cleanup_evidence,
            error=response.correction or "Runner blocked the assignment",
            metadata=self.metadata,
        )

    # -----------------------------------------------------------------------
    # Completion / failure / block
    # -----------------------------------------------------------------------

    def complete(
        self,
        packet: CompletionPacket,
    ) -> PoolWorkerRuntime:
        """Complete the assignment with a structured packet.

        Allowed from: IMPLEMENTING, PLAN_APPROVED, INTERPRETATION_APPROVED,
        PARTIAL_RESULT_APPROVED.
        """
        self._require_state(
            PoolRuntimeState.IMPLEMENTING,
            PoolRuntimeState.PLAN_APPROVED,
            PoolRuntimeState.INTERPRETATION_APPROVED,
            PoolRuntimeState.PARTIAL_RESULT_APPROVED,
        )
        packet.validate(
            expected_run_id=self.assignment.run_id,
            expected_role=self.assignment.role,
            expected_task_id=self.assignment.task_id,
        )
        return PoolWorkerRuntime(
            assignment=self.assignment,
            worker_id=self.worker_id,
            state=PoolRuntimeState.COMPLETING,
            last_checkpoint=self.last_checkpoint,
            last_response=self.last_response,
            completion=packet,
            cleanup_evidence=self.cleanup_evidence,
            error=None,
            metadata=self.metadata,
        )

    def finalize_completion(self) -> PoolWorkerRuntime:
        """Transition from COMPLETING to COMPLETED."""
        self._require_state(PoolRuntimeState.COMPLETING)
        return self._transition(PoolRuntimeState.COMPLETED)

    def block(
        self,
        *,
        reason: str,
        run_id: str | None = None,
    ) -> PoolWorkerRuntime:
        """Transition to BLOCKED (terminal non-failure).

        Allowed from any non-terminal state.
        """
        if run_id is not None:
            self.validate_run_id_match(run_id=run_id)
        terminal = PoolRuntimeState.terminal_states()
        if self.state in terminal:
            raise PoolRuntimeError(
                f"Cannot block from terminal state {self.state.value}"
            )
        if not reason or not reason.strip():
            raise PoolRuntimeError("reason is required for block")
        return PoolWorkerRuntime(
            assignment=self.assignment,
            worker_id=self.worker_id,
            state=PoolRuntimeState.BLOCKED,
            last_checkpoint=self.last_checkpoint,
            last_response=self.last_response,
            completion=None,
            cleanup_evidence=self.cleanup_evidence,
            error=reason,
            metadata=self.metadata,
        )

    def fail(
        self,
        *,
        reason: str,
        run_id: str | None = None,
    ) -> PoolWorkerRuntime:
        """Transition to FAILED (terminal failure).

        Allowed from any non-terminal state.
        """
        if run_id is not None:
            self.validate_run_id_match(run_id=run_id)
        terminal = PoolRuntimeState.terminal_states()
        if self.state in terminal:
            raise PoolRuntimeError(
                f"Cannot fail from terminal state {self.state.value}"
            )
        if not reason or not reason.strip():
            raise PoolRuntimeError("reason is required for fail")
        return PoolWorkerRuntime(
            assignment=self.assignment,
            worker_id=self.worker_id,
            state=PoolRuntimeState.FAILED,
            last_checkpoint=self.last_checkpoint,
            last_response=self.last_response,
            completion=None,
            cleanup_evidence=self.cleanup_evidence,
            error=reason,
            metadata=self.metadata,
        )

    # -----------------------------------------------------------------------
    # Cleanup and release
    # -----------------------------------------------------------------------

    def cleanup(self, evidence: CleanupEvidence) -> PoolWorkerRuntime:
        """Perform cleanup and emit deterministic evidence.

        If evidence is incomplete, this raises PoolCleanupError
        (quarantine trigger).
        """
        terminal = PoolRuntimeState.terminal_states()
        if self.state not in terminal:
            raise PoolRuntimeError(
                f"Cannot cleanup from non-terminal state {self.state.value}; "
                f"must be in one of: {', '.join(s.value for s in terminal)}"
            )
        if not evidence.is_complete():
            missing = evidence.missing_fields()
            raise PoolCleanupError(
                f"Cleanup evidence incomplete; missing fields: "
                f"{', '.join(sorted(missing))}. "
                f"Assignment {self.assignment.assignment_id} must be quarantined."
            )
        return PoolWorkerRuntime(
            assignment=self.assignment,
            worker_id=self.worker_id,
            state=PoolRuntimeState.CLEANED_UP,
            last_checkpoint=self.last_checkpoint,
            last_response=self.last_response,
            completion=self.completion,
            cleanup_evidence=evidence,
            error=self.error,
            metadata=self.metadata,
        )

    def release(self) -> PoolWorkerRuntime:
        """Release from CLEANED_UP to RELEASED."""
        self._require_state(PoolRuntimeState.CLEANED_UP)
        return self._transition(PoolRuntimeState.RELEASED)

    def quarantine(self) -> PoolWorkerRuntime:
        """Move to QUARANTINED from a terminal state.

        This is the fail-safe outcome when cleanup fails or cannot proceed
        safely.
        """
        terminal = PoolRuntimeState.terminal_states()
        if self.state not in terminal:
            raise PoolRuntimeError(
                f"Cannot quarantine from {self.state.value}; "
                f"must be in a terminal state"
            )
        return self._transition(PoolRuntimeState.QUARANTINED)

    # -----------------------------------------------------------------------
    # Inspection helpers
    # -----------------------------------------------------------------------

    def can_accept_assignments(self) -> bool:
        """A worker can accept a new assignment only after Core release."""
        return self.state == PoolRuntimeState.RELEASED

    def is_terminal(self) -> bool:
        return self.state in PoolRuntimeState.terminal_states()

    def is_failed(self) -> bool:
        return self.state in PoolRuntimeState.failed_states()

    def is_success(self) -> bool:
        return self.state in PoolRuntimeState.success_states()

    def quarantine_required(self) -> bool:
        """True if the worker is in a terminal state but cleanup evidence
        was not confirmed."""
        return self.state in PoolRuntimeState.terminal_states() and \
            self.cleanup_evidence is None

    def detect_post_terminal_busy(
        self, *, active_assignment_count: int
    ) -> PostTerminalBusyLeak | None:
        """Detect post-terminal pool-state leaks.

        A pool member in a terminal busy-leak state (COMPLETED, BLOCKED,
        FAILED, QUARANTINED) with zero active Core assignments is a leak.
        Members in RELEASED or CLEANED_UP are expected to have no active
        assignments and are not flagged.
        """
        if self.state not in PoolRuntimeState.busy_leak_states():
            return None
        if active_assignment_count > 0:
            return None
        return PostTerminalBusyLeak(
            member_id=self.worker_id,
            state=self.state.value,
            role=self.assignment.role,
            assignment_id=self.assignment.assignment_id,
            active_assignment_count=active_assignment_count,
        )

    def status_summary(self) -> str:
        if self.completion:
            return (
                f"worker={self.worker_id} state={self.state.value} "
                f"assignment={self.assignment.assignment_id} "
                f"role={self.assignment.role} status={self.completion.status} "
                f"summary={self.completion.summary}"
            )
        if self.error:
            return (
                f"worker={self.worker_id} state={self.state.value} "
                f"assignment={self.assignment.assignment_id} "
                f"error={self.error!r}"
            )
        return (
            f"worker={self.worker_id} state={self.state.value} "
            f"assignment={self.assignment.assignment_id}"
        )


# ---------------------------------------------------------------------------
# Runtime registry guidance for pool workers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PoolWorkerProfileGuide:
    """Profile guidance for a pool worker role, without secrets or
    per-task provider overrides.

    This is a lightweight declaration that the runtime registry can use to
    constrain which Hermes profiles map to which pool roles.
    """
    role: str
    runtime_id: str
    profile: str
    provider: str
    model: str
    toolsets: tuple[str, ...] = ("file", "terminal")
    timeout_seconds: int = 600
    allowed_checkpoint_types: tuple[str, ...] = (
        "assignment_ack",
        "interpretation_checkpoint",
        "plan_checkpoint",
        "partial_result_checkpoint",
        "blocked_needs_input",
    )
    cleanup_policy: str = "full"  # "full" | "minimal" | "manual"
    requires_channel_membership: bool = False
    target_channel_id: int | None = None

    def needs_membership_preflight(self) -> bool:
        """True if this role requires an active Channels membership preflight."""
        return self.requires_channel_membership and self.target_channel_id is not None

    def validate(self) -> None:
        errors: list[str] = []
        if self.role not in CANONICAL_WORKER_ROLES:
            canonical = ", ".join(sorted(CANONICAL_WORKER_ROLES))
            errors.append(f"role must be one of: {canonical}; got {self.role!r}")
        if not self.runtime_id or not self.runtime_id.strip():
            errors.append("runtime_id must not be empty")
        if not self.profile or not self.profile.strip():
            errors.append("profile must not be empty")
        if not self.provider or not self.provider.strip():
            errors.append("provider must not be empty")
        if not self.model or not self.model.strip():
            errors.append("model must not be empty")
        if self.cleanup_policy not in ("full", "minimal", "manual"):
            errors.append(
                f"cleanup_policy must be 'full', 'minimal', or 'manual'; "
                f"got {self.cleanup_policy!r}"
            )
        if self.requires_channel_membership and self.target_channel_id is None:
            errors.append(
                "requires_channel_membership is True but target_channel_id is not set"
            )
        if errors:
            raise PoolRuntimeError(
                f"Invalid pool worker profile guide: {'; '.join(errors)}"
            )


# ---------------------------------------------------------------------------
# Operational wiring: post-terminal reconciliation and profile health check
# ---------------------------------------------------------------------------


def reconcile_pool_members(
    *,
    members: Sequence[Mapping[str, Any]],
    active_assignments_by_member: Mapping[str, int] | None = None,
) -> list[PostTerminalBusyLeak]:
    """Reconcile pool members, detecting post-terminal busy-without-active-assignment leaks.

    Each member dict should contain at least: ``member_id`` (str), ``state`` (str or
    PoolRuntimeState), and optionally ``role``, ``assignment_id``.  Returns a list of
    ``PostTerminalBusyLeak`` detected objects (may be empty).

    This is fakeable: just pass mapped member dicts and assignment counts.
    """
    assignment_counts = dict(active_assignments_by_member or {})
    leaks: list[PostTerminalBusyLeak] = []
    for member in members:
        member_id = str(member.get("member_id") or member.get("worker_id") or "")
        if not member_id:
            continue
        state_raw = member.get("state", "")
        if isinstance(state_raw, PoolRuntimeState):
            state = state_raw
        else:
            try:
                state = PoolRuntimeState(str(state_raw))
            except ValueError:
                continue
        if state not in PoolRuntimeState.busy_leak_states():
            continue
        active_count = assignment_counts.get(member_id, 0)
        if active_count > 0:
            continue
        leak = PostTerminalBusyLeak(
            member_id=member_id,
            state=state.value,
            role=str(member.get("role") or ""),
            assignment_id=str(member.get("assignment_id") or ""),
            active_assignment_count=active_count,
        )
        leaks.append(leak)
    return leaks


# Fakeable profile health check result (no real provider calls).
@dataclass(frozen=True)
class ProfileHealthResult:
    """Result of a fakeable profile/provider health check."""

    profile: str
    provider: str
    model: str
    healthy: bool
    category: str | None = None
    detail: str = ""

    def is_healthy(self) -> bool:
        return self.healthy

    def to_diagnostic(self, *, member_id: str) -> PoolMemberDiagnostic | None:
        if self.healthy:
            return None
        return PoolMemberDiagnostic(
            category=self.category or "auth_unhealthy",
            member_id=member_id,
            evidence={
                "profile": self.profile,
                "provider": self.provider,
                "model": self.model,
                "detail": self.detail,
            },
            recovery="Refresh OAuth token, rotate API key, or check provider status",
        )


def check_profile_health(
    *,
    profile: str,
    provider: str,
    model: str,
    health_fn: Callable[[str, str, str], tuple[bool, str]] | None = None,
) -> ProfileHealthResult:
    """Check profile/provider health (fakeable).

    In production the ``health_fn`` callable can make a lightweight provider
    health check (e.g. list-models, small preflight call).  In tests, pass a
    fake that returns ``(True, "")`` or ``(False, "expired token")``.
    """
    if health_fn is None:
        return ProfileHealthResult(
            profile=profile, provider=provider, model=model,
            healthy=True, detail="health check not configured",
        )
    healthy, detail = health_fn(profile, provider, model)
    return ProfileHealthResult(
        profile=profile,
        provider=provider,
        model=model,
        healthy=healthy,
        category="auth_unhealthy" if not healthy else None,
        detail=detail,
    )

"""Den-specific backstop for Hermes iteration-budget exhaustion.

When a Den-managed spawned worker exhausts its ``max_iterations`` / iteration budget
before completing, this module detects the condition and emits a structured Den
failure signal.  For operator-owned roles (runner, admin, planner,
project_orchestrator) it also emits a Core user notification so the operator is
promptly aware.

Key design invariants:
- Only activates for Den-managed runs with known project/task/run context.
- Ad hoc Hermes CLI sessions without Den context are never notified.
- Deduplicated by (project_id, task_id, run_id) to prevent spam.
- This is a runtime *backstop* — agents should proactively notify near budget;
  this fires only after exact exhaustion.
- Narrow worker roles (coder, reviewer, etc.) get a structured failure signal
  but NOT a direct user notification — the Runner/orchestrator decides.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OPERATOR_NOTIFICATION_ROLES: frozenset[str] = frozenset({
    "runner",
    "admin",
    "planner",
    "project_orchestrator",
})

BUDGET_EXHAUSTION_KEYWORDS: frozenset[str] = frozenset({
    "max_iterations",
    "iteration budget",
    "maximum iterations",
    "maximum number of tool-calling iterations",
    "max turns",
    "turn budget",
    "tool_budget_exhausted",
    "budget exhausted",
    "iteration_budget_exhausted",
    "max_iterations_exhausted",
    "max_iterations reached",
})

# Precompiled pattern for fast keyword matching.
_BUDGET_KEYWORD_PATTERN = re.compile(
    "|".join(re.escape(kw) for kw in BUDGET_EXHAUSTION_KEYWORDS),
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BudgetExhaustionSignal:
    """Structured signal indicating a Den-managed worker hit its iteration budget."""

    project_id: str
    task_id: int
    run_id: str
    role: str
    detection_method: str
    worker_status: str
    error_summary: str
    agent_identity: str


@dataclass
class BudgetExhaustionDeduper:
    """In-memory deduplication guard for budget-exhaustion signals.

    Prevents repeated emission for the same (project_id, task_id, run_id)
    triple within a single bridge process.
    """

    _signaled: set[tuple[str, int, str]] = field(default_factory=set, init=False)

    def is_already_signaled(self, project_id: str, task_id: int, run_id: str) -> bool:
        return (project_id, task_id, run_id) in self._signaled

    def record_signaled(self, project_id: str, task_id: int, run_id: str) -> None:
        self._signaled.add((project_id, task_id, run_id))


@dataclass(frozen=True)
class BudgetExhaustionEmissionEvidence:
    """Evidence returned from a budget-exhaustion signal emission."""

    failure_packet_posted: bool
    user_notification_posted: bool
    dedupe_suppressed: bool
    signal: BudgetExhaustionSignal | None = None


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def detect_budget_exhaustion(
    *,
    worker_result: Any,
    project_id: str | None,
    task_id: int | None,
    run_id: str | None,
    role: str | None,
    agent_identity: str | None,
) -> BudgetExhaustionSignal | None:
    """Examine a worker result for indicators of iteration-budget exhaustion.

    Returns a ``BudgetExhaustionSignal`` when budget exhaustion is the most
    likely cause of the worker's non-completion, or ``None`` otherwise.

    This is intentionally conservative: it returns ``None`` when the signal
    is ambiguous (e.g., a crash with a generic error message).
    """
    # Only activate for Den-managed runs with full context.
    if not project_id or not task_id or not run_id or not role:
        return None

    worker_status = getattr(worker_result, "status", None)
    if worker_status is None:
        return None

    # Completed workers did not exhaust their budget.
    if worker_status == "completed":
        return None

    error_summary = str(getattr(worker_result, "error", "") or "")
    exit_code = getattr(worker_result, "exit_code", None)

    detection_method = _classify_detection(
        worker_status=worker_status,
        error_summary=error_summary,
        exit_code=exit_code,
    )
    if detection_method is None:
        return None

    return BudgetExhaustionSignal(
        project_id=project_id,
        task_id=task_id,
        run_id=run_id,
        role=role,
        detection_method=detection_method,
        worker_status=worker_status,
        error_summary=error_summary,
        agent_identity=agent_identity or role,
    )


def _classify_detection(
    *,
    worker_status: str,
    error_summary: str,
    exit_code: int | None,
) -> str | None:
    """Determine the detection method for budget exhaustion.

    Returns one of:
    - "keyword_match": explicit budget/iteration keyword in the error summary
    - "artifact_status": worker status is "incomplete" (clean exit, no artifact)
    - "missing_artifact_inferred": worker exited 0 but failed validation

    Returns None if the signals don't suggest budget exhaustion.
    """
    # 1. Explicit keyword match in error summary (highest confidence).
    if error_summary and _BUDGET_KEYWORD_PATTERN.search(error_summary):
        return "keyword_match"

    # 2. Worker status is "incomplete" — Hermes exited cleanly (code 0) but
    #    the agent didn't finish. This is the classic budget-exhaustion case.
    if worker_status == "incomplete":
        return "artifact_status"

    # 3. Worker exited successfully but artifact validation failed, AND
    #    there's a hint of iteration/budget in the error.
    if exit_code == 0 and worker_status == "failed" and error_summary:
        # Only infer budget exhaustion for "missing artifact" type failures
        # from clean exits — not crashes.
        if "missing completion artifact" in error_summary.lower():
            return "missing_artifact_inferred"

    return None


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------

def emit_budget_exhaustion_signal(
    *,
    adapter: Any,
    signal: BudgetExhaustionSignal,
    deduper: BudgetExhaustionDeduper,
) -> BudgetExhaustionEmissionEvidence:
    """Emit a structured budget-exhaustion failure signal through the adapter.

    For operator-owned roles, also emits a Core user notification.

    Deduped by (project_id, task_id, run_id): repeated calls for the same
    run are silently suppressed.
    """
    if deduper.is_already_signaled(signal.project_id, signal.task_id, signal.run_id):
        logger.debug(
            "Budget-exhaustion signal suppressed by deduper: "
            "project=%s task=%s run=%s",
            signal.project_id,
            signal.task_id,
            signal.run_id,
        )
        return BudgetExhaustionEmissionEvidence(
            failure_packet_posted=False,
            user_notification_posted=False,
            dedupe_suppressed=True,
        )

    # --- 1. Post structured failure packet ---
    failure_message = (
        f"Hermes iteration budget exhausted for {signal.role} worker "
        f"(run {signal.run_id}, detection: {signal.detection_method}): "
        f"{signal.error_summary or 'worker did not complete before iteration limit'}"
    )
    logger.warning(
        "Budget exhaustion detected: project=%s task=%s run=%s role=%s method=%s",
        signal.project_id,
        signal.task_id,
        signal.run_id,
        signal.role,
        signal.detection_method,
    )

    failure_posted = _post_failure_packet(
        adapter=adapter,
        signal=signal,
        failure_message=failure_message,
    )

    # --- 2. Optional user notification for operator roles ---
    notification_posted = False
    if signal.role in OPERATOR_NOTIFICATION_ROLES:
        notification_posted = _post_user_notification(
            adapter=adapter,
            signal=signal,
            failure_message=failure_message,
        )

    # --- 3. Record dedup ---
    deduper.record_signaled(signal.project_id, signal.task_id, signal.run_id)

    return BudgetExhaustionEmissionEvidence(
        failure_packet_posted=failure_posted,
        user_notification_posted=notification_posted,
        dedupe_suppressed=False,
        signal=signal,
    )


def _post_failure_packet(
    *,
    adapter: Any,
    signal: BudgetExhaustionSignal,
    failure_message: str,
) -> bool:
    """Post a worker failure packet with budget-exhaustion category."""
    try:
        adapter.mark_worker_failed(
            task_id=signal.task_id,
            run_id=signal.run_id,
            role=signal.role,
            error=failure_message,
            failure_category="tool_budget_exhausted",
            recovery_guidance=(
                "The Hermes worker exhausted its iteration/tool-call budget before "
                "writing the expected completion artifact. Increase max_iterations, "
                "split the task into smaller scope, or rerun with additional guidance."
            ),
            dedupe_key=f"{signal.run_id}:tool_budget_exhausted",
        )
        return True
    except Exception:
        logger.warning(
            "Failed to post budget-exhaustion failure packet for run %s; "
            "continuing.",
            signal.run_id,
            exc_info=True,
        )
        return False


def _post_user_notification(
    *,
    adapter: Any,
    signal: BudgetExhaustionSignal,
    failure_message: str,
) -> bool:
    """Emit a user notification for operator roles."""
    try:
        metadata: dict[str, Any] = {
            "type": "tool_budget_exhausted",
            "notification_class": "operator_attention",
            "agent_identity": signal.agent_identity,
            "project_id": signal.project_id,
            "task_id": signal.task_id,
            "run_id": signal.run_id,
            "role": signal.role,
            "detection_method": signal.detection_method,
            "worker_status": signal.worker_status,
        }
        adapter.send_user_notification(
            content=(
                f"[BUDGET EXHAUSTED] Agent {signal.agent_identity} exhausted "
                f"iteration budget on task #{signal.task_id} "
                f"(run {signal.run_id}, role {signal.role}). "
                f"Detection: {signal.detection_method}. "
                f"Increase max_iterations or simplify task scope."
            ),
            task_id=signal.task_id,
            metadata=metadata,
            urgency="high",
        )
        return True
    except Exception:
        logger.warning(
            "Failed to emit budget-exhaustion user notification for run %s; "
            "continuing.",
            signal.run_id,
            exc_info=True,
        )
        return False

"""Emit agent work-complete user notifications from orchestrator drain boundaries.

When the spawned-Hermes Runner/orchestrator finishes its assigned work-drain and
resolves to a terminal action (DONE / BLOCKED / FAILED), this module produces
exactly ONE ``agent_work_complete`` user notification via Core
``send_user_notification``.  The notification is a short pointer/summary, not
the full log, and carries structured metadata for the notification feed.

Key design invariants:
- One notification per top-level drain cycle, never per child worker packet.
- ``source_refs`` is a native JSON array (not a JSON-encoded string).
- ``final_status`` uses contract values ``completed``, ``blocked``, ``failed``
  (not raw enum values like ``done``).
- Local drain-level idempotency guard prevents repeated emission in one
  invocation even if the terminal state is observed multiple times.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Action-type → contract-status mapping
# ---------------------------------------------------------------------------

_TERMINAL_STATUS_MAP: dict[str, str] = {
    "done": "completed",
    "blocked": "blocked",
    "failed": "failed",
}


def _final_status_for_action(action_type_value: str) -> str | None:
    """Map an ``OrchestratorActionType`` value to a contract ``final_status``.

    Returns ``None`` for non-terminal action types (e.g. ``start_coder``).
    """
    return _TERMINAL_STATUS_MAP.get(action_type_value)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WorkCompleteNotification:
    """Structured payload for an ``agent_work_complete`` user notification."""

    agent_identity: str
    completion_scope: str  # "assigned_queue", "single_request", "work_drain"
    final_status: str      # "completed", "blocked", "failed"
    project_ids: list[str]
    task_ids: list[int]
    completed_task_ids: list[int]
    blocked_task_ids: list[int]
    run_ids: list[str]
    source_refs: list[dict[str, Any]]  # native array of structured refs


@dataclass
class WorkCompleteEmissionGuard:
    """Per-drain idempotency guard.  Allows at most one emission per instance.

    The guard is instantiated once per top-level drain cycle and prevents
    repeated notification when the same terminal state is observed again
    within that invocation.  Persistent cross-invocation dedupe is not
    currently supported — see ``known_gaps`` in the completion packet.
    """

    _emitted: bool = field(default=False, init=False)

    @property
    def emitted(self) -> bool:
        return self._emitted

    def mark_emitted(self) -> None:
        self._emitted = True


# ---------------------------------------------------------------------------
# Notification body
# ---------------------------------------------------------------------------

def _notification_body(notification: WorkCompleteNotification) -> str:
    """Generate concise human-readable notification content."""
    status_label = notification.final_status.upper()
    scope_label = notification.completion_scope

    task_parts: list[str] = []
    for tid in notification.task_ids:
        if tid in notification.completed_task_ids:
            task_parts.append(f"#{tid} (completed)")
        elif tid in notification.blocked_task_ids:
            task_parts.append(f"#{tid} ({notification.final_status})")
        else:
            task_parts.append(f"#{tid}")
    tasks_line = ", ".join(task_parts) if task_parts else "n/a"

    runs_line = ""
    if notification.run_ids:
        runs_line = f", run {notification.run_ids[0]}"

    return f"[{status_label}] Agent {notification.agent_identity} {scope_label}\nTasks: {tasks_line}{runs_line}"


# ---------------------------------------------------------------------------
# Emitter
# ---------------------------------------------------------------------------

def emit_work_complete_notification(
    adapter: Any,
    notification: WorkCompleteNotification,
    *,
    guard: WorkCompleteEmissionGuard | None = None,
) -> Any:
    """Emit a work-complete user notification through the adapter.

    Parameters
    ----------
    adapter:
        A ``DenWorkflowAdapter`` (or test fake) with ``send_user_notification``.
    notification:
        The structured notification payload.
    guard:
        Optional per-drain idempotency guard.  When provided, the emission
        will be silently skipped if the guard has already recorded an emission.

    Returns
    -------
    The response from ``send_user_notification``, or ``None`` if the guard
    suppressed the emission.
    """
    if guard is not None and guard.emitted:
        logger.debug(
            "Work-complete emission suppressed by idempotency guard: "
            "agent=%s task_ids=%s final_status=%s",
            notification.agent_identity,
            notification.task_ids,
            notification.final_status,
        )
        return None

    metadata: dict[str, Any] = {
        "type": "agent_work_complete",
        "notification_class": "operator_attention",
        "agent_identity": notification.agent_identity,
        "completion_scope": notification.completion_scope,
        "final_status": notification.final_status,
        "project_ids": notification.project_ids,
        "task_ids": notification.task_ids,
        "completed_task_ids": notification.completed_task_ids,
        "blocked_task_ids": notification.blocked_task_ids,
        "run_ids": notification.run_ids,
        "source_refs": notification.source_refs,  # native array, not JSON string
    }

    urgency = "normal" if notification.final_status == "completed" else "high"

    # Use the first project/task as the primary anchor
    task_id = notification.task_ids[0] if notification.task_ids else 0

    logger.info(
        "Emitting agent_work_complete notification: final_status=%s task=%s urgency=%s",
        notification.final_status,
        task_id,
        urgency,
    )

    result = adapter.send_user_notification(
        content=_notification_body(notification),
        task_id=task_id,
        metadata=metadata,
        urgency=urgency,
    )

    if guard is not None:
        guard.mark_emitted()

    return result

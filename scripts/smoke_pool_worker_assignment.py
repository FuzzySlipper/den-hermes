#!/usr/bin/env python3
"""No-mutation smoke helper: demonstrates assignment-pointer processing and
acknowledgment for the five live task-worker roles using the existing
PoolWorkerRuntime state machine.

This script is **fully deterministic**: no network I/O, no Den API calls,
no file mutations. It creates in-memory PoolWorkerRuntime instances for
each role, runs through acknowledge(), and records role-specific packet
expectations.

The output is a structured JSON report with handles that Runner can
connect to Core/assignment records during live application.

Usage:
    python scripts/smoke_pool_worker_assignment.py                    # all roles
    python scripts/smoke_pool_worker_assignment.py --roles reviewer,validator
    python scripts/smoke_pool_worker_assignment.py --json             # JSON output

Exit codes:
    0 – all roles smoked successfully.
    1 – one or more roles failed.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Sequence

# Make direct script execution work without requiring PYTHONPATH=.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from den_hermes.pool_runtime import (
    AssignmentPointer,
    PoolRuntimeState,
    PoolWorkerRuntime,
)

# ---------------------------------------------------------------------------
# Role-specific metadata from docs/worker-role-catalog.md
# ---------------------------------------------------------------------------

ROLE_PACKET_TYPES: dict[str, str] = {
    "coder": "implementation_packet",
    "reviewer": "review_findings_packet",
    "validator": "validation_packet",
    "drift_checker": "drift_check_packet",
    "packet_auditor": "packet_audit_packet",
}

ROLE_CAPABILITY_TAGS: dict[str, list[str]] = {
    "coder": ["implementation", "code_generation"],
    "reviewer": ["review", "code_audit"],
    "validator": ["validation", "test_verification"],
    "drift_checker": ["drift_detection", "consistency_check"],
    "packet_auditor": ["audit", "packet_verification"],
}

ROLE_CHECKPOINT_TYPES: dict[str, list[str]] = {
    "coder": ["assignment_ack", "interpretation_checkpoint", "plan_checkpoint", "blocked_needs_input"],
    "reviewer": ["assignment_ack", "blocked_needs_input"],
    "validator": ["assignment_ack", "blocked_needs_input"],
    "drift_checker": ["assignment_ack", "blocked_needs_input"],
    "packet_auditor": ["assignment_ack", "blocked_needs_input"],
}

ROLE_POOL_MEMBER_PREFIXES: dict[str, str] = {
    "coder": "pool-coder",
    "reviewer": "pool-reviewer",
    "validator": "pool-validator",
    "drift_checker": "pool-drift-checker",
    "packet_auditor": "pool-packet-auditor",
}


# ---------------------------------------------------------------------------
# Smoke result data
# ---------------------------------------------------------------------------


@dataclass
class RoleSmokeResult:
    """Single role smoke result."""

    role: str
    pool_member_id: str
    run_id: str
    assignment_id: str
    task_id: int
    project_id: str
    initial_state: str
    assignment_metadata: dict[str, Any] = field(default_factory=dict)
    acknowledged_state: str | None = None
    ack_checkpoint_type: str | None = None
    packet_type: str | None = None
    capabilities: list[str] = field(default_factory=list)
    allowed_checkpoints: list[str] = field(default_factory=list)
    interpretation_summary: str | None = None
    error: str | None = None
    success: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SmokeReport:
    """Collective smoke report."""

    roles_smoked: list[RoleSmokeResult] = field(default_factory=list)
    roles_total: int = 0
    roles_passed: int = 0
    roles_failed: int = 0
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core smoke logic
# ---------------------------------------------------------------------------


def smoke_role(
    role: str,
    *,
    task_id: int = 1784,
    project_id: str = "den-hermes-bridge",
    run_id: str | None = None,
    slot_number: int = 1,
) -> RoleSmokeResult:
    """Run a no-mutation smoke for a single pool-worker role.

    Creates an AssignmentPointer and PoolWorkerRuntime, validates the
    assignment, acknowledges it, and records role-specific packet/checkpoint
    metadata.

    NOTE: ``task_id=1784`` and ``project_id="den-hermes-bridge"`` are smoke
    defaults only — they define the pilot provisioning control channel, not
    the structural work-attribution model.  Cross-project assignments must
    carry explicit ``target_project_id`` / ``target_task_id`` metadata
    (see #1834).

    Returns a RoleSmokeResult with handles that Runner can connect to
    Core/assignment records during live application.
    """
    if role not in ROLE_PACKET_TYPES:
        return RoleSmokeResult(
            role=role,
            pool_member_id="",
            run_id=run_id or f"t{task_id}-{role}-slot{slot_number:02d}-smoke-000000",
            assignment_id="",
            task_id=task_id,
            project_id=project_id,
            initial_state=PoolRuntimeState.PENDING.value,
            error=f"Unknown role: {role!r}",
            success=False,
        )

    if slot_number < 1:
        raise ValueError(f"slot_number must be >= 1, got {slot_number!r}")

    prefix = ROLE_POOL_MEMBER_PREFIXES.get(role, f"pool-{role}")
    pool_member_id = f"{prefix}-{slot_number:02d}"

    effective_run_id = run_id or f"t{task_id}-{role}-slot{slot_number:02d}-smoke-000000"

    assignment_metadata = {
        "smoke": True,
        "provisioning": {
            "source": "smoke_pool_worker_assignment.py",
            "task_id": task_id,
        },
    }

    assignment = AssignmentPointer(
        assignment_id=f"t{task_id}-assign-{role}-smoke",
        task_id=task_id,
        run_id=effective_run_id,
        role=role,
        project_id=project_id,
        metadata=assignment_metadata,
    )

    result = RoleSmokeResult(
        role=role,
        pool_member_id=pool_member_id,
        run_id=effective_run_id,
        assignment_id=assignment.assignment_id,
        task_id=task_id,
        project_id=project_id,
        initial_state=PoolRuntimeState.PENDING.value,
        assignment_metadata=assignment_metadata,
        packet_type=ROLE_PACKET_TYPES.get(role, f"{role}_packet"),
        capabilities=ROLE_CAPABILITY_TAGS.get(role, [role]),
        allowed_checkpoints=ROLE_CHECKPOINT_TYPES.get(role, ["assignment_ack", "blocked_needs_input"]),
    )

    try:
        # 1. Validate assignment
        assignment.validate()

        # 2. Create runtime (PENDING)
        runtime = PoolWorkerRuntime(
            assignment=assignment,
            worker_id=pool_member_id,
        )
        assert runtime.state == PoolRuntimeState.PENDING
        assert runtime.can_accept_assignments() is False
        assert runtime.is_terminal() is False

        # 3. Acknowledge with role-appropriate interpretation summary
        interpretation_summary = (
            f"Smoke acknowledge for {role} role. "
            f"Packet type: {result.packet_type}. "
            f"Capabilities: {', '.join(result.capabilities)}."
        )

        runtime = runtime.acknowledge(
            interpretation_summary=interpretation_summary,
            uncertainties=["Smoke run: no active Core delivery"],
            non_goals=["No actual code changes or Den API calls"],
        )

        assert runtime.state == PoolRuntimeState.ACKNOWLEDGED
        assert runtime.last_checkpoint is not None
        assert runtime.last_checkpoint.type == "assignment_ack"
        assert runtime.last_checkpoint.role == role
        assert runtime.last_checkpoint.run_id == effective_run_id
        assert runtime.last_checkpoint.task_id == task_id

        result.acknowledged_state = PoolRuntimeState.ACKNOWLEDGED.value
        result.ack_checkpoint_type = "assignment_ack"
        result.interpretation_summary = interpretation_summary
        result.success = True

    except Exception as exc:
        result.error = str(exc)
        result.success = False

    return result


def run_smoke(
    *,
    roles: Sequence[str] | None = None,
    task_id: int = 1784,
    project_id: str = "den-hermes-bridge",
    run_id: str | None = None,
    slot_number: int = 1,
) -> SmokeReport:
    """Run smoke for all specified roles."""
    if roles is None:
        roles = tuple(ROLE_PACKET_TYPES.keys())

    report = SmokeReport()
    report.roles_total = len(roles)

    for role in roles:
        if role not in ROLE_PACKET_TYPES:
            report.errors.append(f"Unknown role: {role!r}")
            report.roles_failed += 1
            continue

        result = smoke_role(
            role,
            task_id=task_id,
            project_id=project_id,
            run_id=run_id,
            slot_number=slot_number,
        )
        report.roles_smoked.append(result)
        if result.success:
            report.roles_passed += 1
        else:
            report.roles_failed += 1

    return report


def format_summary(report: SmokeReport) -> str:
    """Format a human-readable smoke report summary."""
    lines = [
        f"Pool Worker Assignment Smoke Summary",
        f"Roles total: {report.roles_total}",
        f"Roles passed: {report.roles_passed}",
        f"Roles failed: {report.roles_failed}",
        "",
    ]

    if report.roles_smoked:
        lines.append(
            f"{'ROLE':<18} {'POOL MEMBER':<22} {'STATE':<16} {'PACKET TYPE':<28} {'STATUS':<10} RUN_ID"
        )
        lines.append(
            f"{'----':<18} {'-----------':<22} {'-----':<16} {'-----------':<28} {'------':<10} ------"
        )
    for r in report.roles_smoked:
        state = r.acknowledged_state or r.initial_state
        status = "PASS" if r.success else "FAIL"
        lines.append(
            f"{r.role:<18} {r.pool_member_id:<22} {state:<16} "
            f"{r.packet_type:<28} {status:<10} {r.run_id}"
        )
        if not r.success and r.error:
            lines.append(f"  ERROR: {r.error}")

    if report.errors:
        lines.append("")
        lines.append("GLOBAL ERRORS:")
        for err in report.errors:
            lines.append(f"  ! {err}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="smoke_pool_worker_assignment",
        description="No-mutation smoke helper for pool-worker assignment ack",
    )
    parser.add_argument(
        "--roles",
        default=",".join(ROLE_PACKET_TYPES.keys()),
        help="Comma-separated roles to smoke (default: all five live task-worker roles)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output machine-readable JSON instead of human-readable table",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Override the default smoke run_id",
    )
    parser.add_argument(
        "--task-id",
        default=1784,
        type=int,
        help="Task ID for smoke assignments (default: 1784)",
    )
    parser.add_argument(
        "--slot-number",
        default=1,
        type=int,
        help="Concrete pool slot number to smoke (default: 1)",
    )
    parser.add_argument(
        "--project-id",
        default="den-hermes-bridge",
        help="Project ID for smoke assignments (default: den-hermes-bridge)",
    )

    args = parser.parse_args(argv)
    roles = [r.strip() for r in args.roles.split(",") if r.strip()]

    report = run_smoke(
        roles=roles,
        task_id=args.task_id,
        project_id=args.project_id,
        run_id=args.run_id,
        slot_number=args.slot_number,
    )

    if args.json:
        print(json.dumps({
            "roles_total": report.roles_total,
            "roles_passed": report.roles_passed,
            "roles_failed": report.roles_failed,
            "errors": report.errors,
            "results": [r.to_dict() for r in report.roles_smoked],
        }, indent=2))
    else:
        print(format_summary(report))

    return 1 if report.roles_failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())

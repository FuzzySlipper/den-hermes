#!/usr/bin/env python3
"""Validate the worker pool MVP rollout runbook doc for required sections, terms, and structure.

This script is fully deterministic: no network I/O, no Den API calls,
pure text and structural checks.

Usage:
    python scripts/validate_worker_pool_mvp_runbook.py
"""

from __future__ import annotations

import sys
from pathlib import Path

DOC_PATH = Path(__file__).parent.parent / "docs" / "worker-pool-mvp-rollout-runbook.md"

REQUIRED_TERMS = [
    "worker pool",
    "one-shot spawned-Hermes",
    "delegate_task",
    "stuck lease",
    "release",
    "quarantine",
    "Den Web",
    "Gateway delivery",
    "Channels wake",
    "checkpoint protocol",
    "PoolWorkerRuntime",
    "PoolRuntimeState",
    "CleanupEvidence",
    "PoolCleanupError",
    "RELEASED",
    "QUARANTINED",
    "cleanup",
    "lease_expires_at",
    "fail-closed",
    "PoolCleanupError",
    "can_accept_assignments",
    "quarantine_required",
    "live-smoke",
    "readback",
    "lesson_id",
    "lesson.source",
    "lesson.verdict",
    "lesson.timestamp",
    "lesson.run_id",
    "lesson.assignment_id",
    "lesson.notes",
    "RED",
    "AMBER",
    "fake E2E",
]

REQUIRED_SECTIONS = [
    "1. Summary",
    "2. Substrate selection guide",
    "2.1 Direct `delegate_task`",
    "2.2 One-shot spawned-Hermes",
    "2.3 Worker pool",
    "2.4 Decision flowchart",
    "3. Diagnosing stuck leases",
    "3.1 Detection steps",
    "3.2 Recovery",
    "4. Release and quarantine",
    "4.1 Release path",
    "4.2 Quarantine path",
    "4.3 When quarantine is triggered",
    "4.4 Operator resolution for quarantine",
    "4.5 State summary matrix",
    "5. Observing an assignment in Den Web",
    "5.1 Trace fields",
    "5.2 How to read the trace",
    "5.3 Correlation with other handles",
    "6. Constrained live-smoke runbook",
    "6.1 Preconditions",
    "6.2 Live-smoke procedure",
    "6.3 Fail-closed criteria",
    "6.4 Smoke lesson fields",
    "6.5 Example lesson record (passed)",
    "6.6 Example lesson record (failed at delivery)",
    "7. Operator troubleshooting",
    "Appendix A",
    "Appendix B",
]


def main() -> int:
    if not DOC_PATH.exists():
        print(f"FAIL: {DOC_PATH} does not exist")
        return 1

    text = DOC_PATH.read_text()
    errors = 0

    print("=== Required terms ===")
    for term in REQUIRED_TERMS:
        if term.lower() not in text.lower():
            print(f"  FAIL: required term not found: {term}")
            errors += 1
        else:
            print(f"  OK: term found: {term}")

    print("\n=== Required sections ===")
    for section in REQUIRED_SECTIONS:
        needles = [
            f"## {section}",
            f"### {section}",
            f"#### {section}",
        ]
        found = any(n in text for n in needles)
        if not found:
            print(f"  FAIL: required section not found: {section}")
            errors += 1
        else:
            print(f"  OK: section found: {section}")

    # Structural checks
    print("\n=== Structure checks ===")

    # Check for required checkboxes in precondition list
    checkbox_count = text.count("- [ ]")
    if checkbox_count < 5:
        print(f"  FAIL: expected at least 5 checkboxes; found {checkbox_count}")
        errors += 1
    else:
        print(f"  OK: {checkbox_count} checkboxes found (live-smoke preconditions)")

    # Check for runbook steps
    step_count = text.count("#### Step")
    if step_count < 10:
        print(f"  FAIL: expected at least 10 runbook steps; found {step_count}")
        errors += 1
    else:
        print(f"  OK: {step_count} runbook steps found")

    # Check for JSON example blocks
    json_block_count = text.count("```json")
    if json_block_count < 1:
        print("  FAIL: expected at least 1 JSON example block")
        errors += 1
    else:
        print(f"  OK: {json_block_count} JSON example blocks found")

    # Check for table markers (at least 5 tables)
    table_count = text.count("| --- |")
    if table_count < 5:
        print(f"  FAIL: expected at least 5 tables; found {table_count}")
        errors += 1
    else:
        print(f"  OK: {table_count} table separator markers found")

    # Check for state transition diagram (Appendix A with arrows)
    arrow_count = text.count("->")
    if arrow_count < 10:
        print(f"  FAIL: expected state transition arrows (->) in Appendix A; found {arrow_count}")
        errors += 1
    else:
        print(f"  OK: {arrow_count} state transition arrows found")

    if errors:
        print(f"\nValidation failed with {errors} error(s).")
        return 1

    print("\nAll validations passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

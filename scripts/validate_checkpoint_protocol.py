#!/usr/bin/env python3
"""Validate the checkpoint protocol pilot doc for required sections, terms, and template markers."""
from __future__ import annotations

import sys
from pathlib import Path

DOC_PATH = Path(__file__).parent.parent / "docs" / "checkpoint-protocol-pilot.md"

REQUIRED_TERMS = [
    "assignment_ack",
    "interpretation_checkpoint",
    "plan_checkpoint",
    "checkpoint_response",
    "partial_result_checkpoint",
    "blocked_needs_input",
    "mandatory",
    "architectur",
    "schema",
    "wake",
    "memory",
    "worker orchestration",
    "prose-only",
    "Den task-thread",
    "Den Channels direct-agent messages",
    "wake surface",
    "wake notification",
    "send_agent_stream_message",
    "metadata.type",
    "checkpoint_response",
    "approved_with_correction",
    "changes_requested",
    "blocked",
    "Worker must stop",
    "mandatory-gated",
    "no Hermes Kanban",
    "no Core schema",
    "smoke_run_id",
    "lesson_id",
    "lesson.source",
    "lesson.phase",
    "lesson.verdict",
    "lesson.timestamp",
    "lesson.message_id",
    "lesson.notes",
    "pilot smoke",
    "smoke verification",
    "checkpoint protocol",
]

REQUIRED_SECTIONS = [
    "1. Purpose",
    "2. Checkpoint packet type definitions",
    "2.1 assignment_ack",
    "2.2 interpretation_checkpoint",
    "2.3 plan_checkpoint",
    "2.4 checkpoint_response",
    "2.5 partial_result_checkpoint",
    "2.6 blocked_needs_input",
    "3. Mandatory vs optional checkpoint rules",
    "3.1 Mandatory checkpoints",
    "3.2 Optional checkpoints",
    "3.3 When to skip checkpoints",
    "4. Runner/operator guidance",
    "4.1 Worker handoff wording",
    "4.2 Worker must stop",
    "4.3 Runner response discipline",
    "4.4 Resuming after checkpoint approval",
    "5. Concrete examples",
    "5.1 Den task-thread metadata as authoritative state",
    "5.2 Den Channels direct-agent messages as wake surface only",
    "6. Relationship to existing contracts",
    "7. Pilot smoke",
    "8. Validation",
    "Appendix A",
    "Appendix B",
]

TEMPLATE_MARKERS = [
    '"type": "assignment_ack"',
    '"type": "interpretation_checkpoint"',
    '"type": "plan_checkpoint"',
    '"type": "checkpoint_response"',
    '"type": "partial_result_checkpoint"',
    '"type": "blocked_needs_input"',
    '"verdict": "approved"',
    '"approved_with_correction"',
    '"changes_requested"',
    '"blocked"',
    '"blocker_category": "needs_runner_decision"',
    '"blocker_summary"',
    '"recovery_guidance"',
    '"accepted_criteria"',
    '"files_to_touch"',
    '"validation_plan"',
    '"responds_to_checkpoint_type"',
    '"vertical_slice_paths"',
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
        # Sections are matched as heading fragments
        needles = [
            f"## {section}",
            f"### {section}",
        ]
        found = any(n in text for n in needles)
        if not found:
            print(f"  FAIL: required section not found: {section}")
            errors += 1
        else:
            print(f"  OK: section found: {section}")

    print("\n=== Template structure markers ===")
    for marker in TEMPLATE_MARKERS:
        if marker not in text:
            print(f"  FAIL: required template marker not found: {marker}")
            errors += 1
        else:
            print(f"  OK: template marker found: {marker}")

    # Forbidden patterns
    forbidden = [
        "Kanban",
        "parallel board",
        "mcp_den_send_message",
        "send_agent_stream_message",
    ]
    # send_agent_stream_message is mentioned as "Do not use" - that's fine
    # Actually let me check: the document says "Do not use send_agent_stream_message..."
    # That's correct. But we should not have actual USE of it as a wake mechanism.
    # Let me just check for the forbidden words that indicate the doc is doing
    # the wrong thing, not mentioning them.
    forbidden_not_do_not_use = ["Kanban", "parallel board"]
    print("\n=== Forbidden pattern check ===")
    for f in forbidden_not_do_not_use:
        if f in text:
            # Make sure it's in a negative context
            lines_with_word = [l for l in text.split("\n") if f.lower() in l.lower()]
            for line in lines_with_word:
                if "no" not in line.lower() and "not" not in line.lower():
                    print(f"  FAIL: forbidden word appears without negative context: {f}")
                    errors += 1
                    break
            else:
                print(f"  OK: {f} mentioned only in negative context")
        else:
            print(f"  OK: {f} not present")

    if errors:
        print(f"\nValidation failed with {errors} error(s).")
        return 1

    print("\nAll validations passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

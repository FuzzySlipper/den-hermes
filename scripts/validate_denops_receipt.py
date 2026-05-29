#!/usr/bin/env python3
"""Validate a DenOps compact receipt against the strict schema.

This script is fully deterministic: no network I/O, no Den API calls,
pure JSON schema and structural checks.

Usage:
    python scripts/validate_denops_receipt.py                    # validates example receipts
    python scripts/validate_denops_receipt.py --receipt <path>   # validates a specific file
    python scripts/validate_denops_receipt.py --all              # validates all example receipts
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).parent
DOCS_EXAMPLES_DIR = SCRIPT_DIR.parent / "docs" / "examples"

# ---------------------------------------------------------------------------
# Allowed values
# ---------------------------------------------------------------------------

VALID_STATUSES = frozenset({"completed", "partial", "blocked", "failed"})

VALID_BLOCKER_CATEGORIES = frozenset({
    "needs_runner_decision",
    "infrastructure",
    "den_unreachable",
    "tool_missing",
    "authentication",
    "unexpected_state",
})

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _fail(msg: str) -> str:
    return f"  FAIL: {msg}"


def _ok(msg: str) -> str:
    return f"  OK: {msg}"


def _error(msg: str, tag: str = "") -> list[str]:
    prefix = f"[{tag}] " if tag else ""
    return [_fail(f"{prefix}{msg}")]


def validate_receipt(receipt: Any, *, tag: str = "") -> list[str]:
    """Validate a single receipt dict against the DenOps schema.

    Returns a list of failure messages (empty = valid).
    """
    errors: list[str] = []

    # -----------------------------------------------------------------------
    # Top-level required fields
    # -----------------------------------------------------------------------
    if not isinstance(receipt, dict):
        return _error("Receipt must be a JSON object", tag)

    # receipt_version
    if "receipt_version" not in receipt:
        errors.extend(_error("Missing required field: receipt_version", tag))
    elif receipt["receipt_version"] != "1.0":
        errors.extend(_error(
            f"receipt_version must be '1.0'; got {receipt['receipt_version']!r}",
            tag,
        ))

    # status
    if "status" not in receipt:
        errors.extend(_error("Missing required field: status", tag))
    elif receipt["status"] not in VALID_STATUSES:
        valid = ", ".join(sorted(VALID_STATUSES))
        errors.extend(_error(
            f"status must be one of: {valid}; got {receipt['status']!r}",
            tag,
        ))

    # summary (string, non-empty)
    if "summary" not in receipt:
        errors.extend(_error("Missing required field: summary", tag))
    elif not isinstance(receipt["summary"], str) or not receipt["summary"].strip():
        errors.extend(_error("summary must be a non-empty string", tag))
    elif len(receipt["summary"]) > 200:
        errors.extend(_error(
            f"summary must be ≤200 characters; got {len(receipt['summary'])}",
            tag,
        ))

    # handles (object)
    if "handles" not in receipt:
        errors.extend(_error("Missing required field: handles", tag))
    else:
        errors.extend(_validate_handles(receipt["handles"], tag=tag))

    # blockers (array)
    if "blockers" not in receipt:
        errors.extend(_error("Missing required field: blockers", tag))
    elif not isinstance(receipt["blockers"], list):
        errors.extend(_error("blockers must be an array", tag))
    else:
        for i, blocker in enumerate(receipt["blockers"]):
            errors.extend(_validate_blocker(blocker, idx=i, tag=tag))

    # Consistency: completed/partial must have empty blockers
    status = receipt.get("status", "")
    blockers = receipt.get("blockers", [])
    if status in ("completed", "partial") and blockers:
        errors.extend(_error(
            f"status is '{status}' but blockers array is non-empty "
            f"(got {len(blockers)} blocker(s))",
            tag,
        ))
    if status in ("blocked", "failed") and not blockers:
        errors.extend(_error(
            f"status is '{status}' but blockers array is empty",
            tag,
        ))

    # assumptions (array)
    if "assumptions" not in receipt:
        errors.extend(_error("Missing required field: assumptions", tag))
    elif not isinstance(receipt["assumptions"], list):
        errors.extend(_error("assumptions must be an array", tag))
    else:
        for i, assumption in enumerate(receipt["assumptions"]):
            errors.extend(_validate_assumption(assumption, idx=i, tag=tag))

    # next_required_action (string, non-empty)
    if "next_required_action" not in receipt:
        errors.extend(_error("Missing required field: next_required_action", tag))
    elif not isinstance(receipt["next_required_action"], str) or not receipt["next_required_action"].strip():
        errors.extend(_error("next_required_action must be a non-empty string", tag))

    # verification (object)
    if "verification" not in receipt:
        errors.extend(_error("Missing required field: verification", tag))
    else:
        errors.extend(_validate_verification(receipt["verification"], tag=tag))

    return errors


# ---------------------------------------------------------------------------
# Sub-validators
# ---------------------------------------------------------------------------


def _validate_handles(handles: Any, *, tag: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(handles, dict):
        return _error("handles must be a JSON object", tag)

    # At least one handle type must have non-empty content
    handle_types = ["tasks", "messages", "documents", "wake_events", "delivery_requests"]
    has_content = False

    for key in handle_types:
        value = handles.get(key)
        if value is not None:
            if not isinstance(value, list):
                errors.extend(_error(f"handles.{key} must be an array", tag))
            else:
                if len(value) > 0:
                    has_content = True
                handlers = {
                    "tasks": _validate_task_handle,
                    "messages": _validate_message_handle,
                    "documents": _validate_document_handle,
                    "wake_events": _validate_wake_event_handle,
                    "delivery_requests": _validate_delivery_request_handle,
                }
                validator = handlers[key]
                for i, item in enumerate(value):
                    errors.extend(validator(item, idx=i, tag=tag))

    if not has_content and "handles" in str(handles):
        # If handles has no non-empty arrays at all, that's OK per schema
        # (handles can be empty for blocked/failed receipts)
        pass

    return errors


def _validate_task_handle(item: Any, *, idx: int, tag: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(item, dict):
        return _error(f"handles.tasks[{idx}] must be an object", tag)

    for field in ("task_id", "project_id", "status"):
        if field not in item:
            errors.extend(_error(f"handles.tasks[{idx}] missing required field: {field}", tag))

    if "task_id" in item and not isinstance(item["task_id"], int):
        errors.extend(_error(
            f"handles.tasks[{idx}].task_id must be an integer; "
            f"got {type(item['task_id']).__name__}",
            tag,
        ))
    if "project_id" in item and (not isinstance(item["project_id"], str) or not item["project_id"].strip()):
        errors.extend(_error(f"handles.tasks[{idx}].project_id must be a non-empty string", tag))
    if "status" in item and (not isinstance(item["status"], str) or not item["status"].strip()):
        errors.extend(_error(f"handles.tasks[{idx}].status must be a non-empty string", tag))
    if "message_count" in item and not isinstance(item["message_count"], int):
        errors.extend(_error(f"handles.tasks[{idx}].message_count must be an integer", tag))
    if "latest_message_id" in item and not isinstance(item["latest_message_id"], int):
        errors.extend(_error(f"handles.tasks[{idx}].latest_message_id must be an integer", tag))
    if "dependencies" in item:
        if not isinstance(item["dependencies"], list):
            errors.extend(_error(f"handles.tasks[{idx}].dependencies must be an array", tag))
        else:
            for j, dep in enumerate(item["dependencies"]):
                if not isinstance(dep, int):
                    errors.extend(_error(
                        f"handles.tasks[{idx}].dependencies[{j}] must be an integer",
                        tag,
                    ))
    return errors


def _validate_message_handle(item: Any, *, idx: int, tag: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(item, dict):
        return _error(f"handles.messages[{idx}] must be an object", tag)

    for field in ("message_id", "type", "summary"):
        if field not in item:
            errors.extend(_error(f"handles.messages[{idx}] missing required field: {field}", tag))

    if "message_id" in item and not isinstance(item["message_id"], int):
        errors.extend(_error(f"handles.messages[{idx}].message_id must be an integer", tag))
    if "type" in item and (not isinstance(item["type"], str) or not item["type"].strip()):
        errors.extend(_error(f"handles.messages[{idx}].type must be a non-empty string", tag))
    if "summary" in item:
        if not isinstance(item["summary"], str) or not item["summary"].strip():
            errors.extend(_error(f"handles.messages[{idx}].summary must be a non-empty string", tag))
        elif len(item["summary"]) > 200:
            errors.extend(_error(
                f"handles.messages[{idx}].summary must be ≤200 characters; "
                f"got {len(item['summary'])}",
                tag,
            ))
    if "timestamp" in item and not isinstance(item["timestamp"], str):
        errors.extend(_error(f"handles.messages[{idx}].timestamp must be a string", tag))

    return errors


def _validate_document_handle(item: Any, *, idx: int, tag: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(item, dict):
        return _error(f"handles.documents[{idx}] must be an object", tag)

    for field in ("document_id", "project_id", "summary"):
        if field not in item:
            errors.extend(_error(f"handles.documents[{idx}] missing required field: {field}", tag))

    if "document_id" in item and (not isinstance(item["document_id"], str) or not item["document_id"].strip()):
        errors.extend(_error(f"handles.documents[{idx}].document_id must be a non-empty string", tag))
    if "project_id" in item and (not isinstance(item["project_id"], str) or not item["project_id"].strip()):
        errors.extend(_error(f"handles.documents[{idx}].project_id must be a non-empty string", tag))
    if "summary" in item and (not isinstance(item["summary"], str) or not item["summary"].strip()):
        errors.extend(_error(f"handles.documents[{idx}].summary must be a non-empty string", tag))
    if "status" in item and not isinstance(item["status"], str):
        errors.extend(_error(f"handles.documents[{idx}].status must be a string", tag))

    return errors


def _validate_wake_event_handle(item: Any, *, idx: int, tag: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(item, dict):
        return _error(f"handles.wake_events[{idx}] must be an object", tag)

    for field in ("event_id", "summary", "status"):
        if field not in item:
            errors.extend(_error(f"handles.wake_events[{idx}] missing required field: {field}", tag))

    if "event_id" in item and (not isinstance(item["event_id"], str) or not item["event_id"].strip()):
        errors.extend(_error(f"handles.wake_events[{idx}].event_id must be a non-empty string", tag))
    if "summary" in item and (not isinstance(item["summary"], str) or not item["summary"].strip()):
        errors.extend(_error(f"handles.wake_events[{idx}].summary must be a non-empty string", tag))
    if "channel_id" in item and not isinstance(item["channel_id"], int):
        errors.extend(_error(f"handles.wake_events[{idx}].channel_id must be an integer", tag))
    if "status" in item and item["status"] not in ("pending", "delivered", "acknowledged"):
        errors.extend(_error(
            f"handles.wake_events[{idx}].status must be one of: "
            f"'pending', 'delivered', 'acknowledged'; got {item['status']!r}",
            tag,
        ))

    return errors


def _validate_delivery_request_handle(item: Any, *, idx: int, tag: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(item, dict):
        return _error(f"handles.delivery_requests[{idx}] must be an object", tag)

    for field in ("request_id", "target_identity", "summary", "status"):
        if field not in item:
            errors.extend(_error(f"handles.delivery_requests[{idx}] missing required field: {field}", tag))

    if "request_id" in item and (not isinstance(item["request_id"], str) or not item["request_id"].strip()):
        errors.extend(_error(f"handles.delivery_requests[{idx}].request_id must be a non-empty string", tag))
    if "target_identity" in item and (not isinstance(item["target_identity"], str) or not item["target_identity"].strip()):
        errors.extend(_error(f"handles.delivery_requests[{idx}].target_identity must be a non-empty string", tag))
    if "summary" in item and (not isinstance(item["summary"], str) or not item["summary"].strip()):
        errors.extend(_error(f"handles.delivery_requests[{idx}].summary must be a non-empty string", tag))
    if "status" in item and item["status"] not in ("posted", "pending", "delivered", "failed"):
        errors.extend(_error(
            f"handles.delivery_requests[{idx}].status must be one of: "
            f"'posted', 'pending', 'delivered', 'failed'; got {item['status']!r}",
            tag,
        ))

    return errors


def _validate_blocker(blocker: Any, *, idx: int, tag: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(blocker, dict):
        return _error(f"blockers[{idx}] must be an object", tag)

    for field in ("blocker_summary", "blocker_category", "recovery_guidance"):
        if field not in blocker:
            errors.extend(_error(f"blockers[{idx}] missing required field: {field}", tag))

    if "blocker_summary" in blocker and (not isinstance(blocker["blocker_summary"], str) or not blocker["blocker_summary"].strip()):
        errors.extend(_error(f"blockers[{idx}].blocker_summary must be a non-empty string", tag))
    if "blocker_category" in blocker and blocker["blocker_category"] not in VALID_BLOCKER_CATEGORIES:
        valid = ", ".join(sorted(VALID_BLOCKER_CATEGORIES))
        errors.extend(_error(
            f"blockers[{idx}].blocker_category must be one of: {valid}; "
            f"got {blocker['blocker_category']!r}",
            tag,
        ))
    if "recovery_guidance" in blocker and (not isinstance(blocker["recovery_guidance"], str) or not blocker["recovery_guidance"].strip()):
        errors.extend(_error(f"blockers[{idx}].recovery_guidance must be a non-empty string", tag))
    if "evidence_handles" in blocker:
        if not isinstance(blocker["evidence_handles"], list):
            errors.extend(_error(f"blockers[{idx}].evidence_handles must be an array", tag))
        else:
            for j, handle in enumerate(blocker["evidence_handles"]):
                if not isinstance(handle, str):
                    errors.extend(_error(
                        f"blockers[{idx}].evidence_handles[{j}] must be a string",
                        tag,
                    ))

    return errors


def _validate_assumption(assumption: Any, *, idx: int, tag: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(assumption, dict):
        return _error(f"assumptions[{idx}] must be an object", tag)

    for field in ("assumption", "verified"):
        if field not in assumption:
            errors.extend(_error(f"assumptions[{idx}] missing required field: {field}", tag))

    if "assumption" in assumption and (not isinstance(assumption["assumption"], str) or not assumption["assumption"].strip()):
        errors.extend(_error(f"assumptions[{idx}].assumption must be a non-empty string", tag))
    if "verified" in assumption and not isinstance(assumption["verified"], bool):
        errors.extend(_error(f"assumptions[{idx}].verified must be a boolean", tag))

    return errors


def _validate_verification(verification: Any, *, tag: str) -> list[str]:
    errors: list[str] = []
    if not isinstance(verification, dict):
        return _error("verification must be a JSON object", tag)

    for field in ("readback_messages_checked", "task_thread_readback", "handle_integrity"):
        if field not in verification:
            errors.extend(_error(f"verification missing required field: {field}", tag))
        elif not isinstance(verification[field], bool):
            errors.extend(_error(f"verification.{field} must be a boolean", tag))

    if "verification_method" in verification and not isinstance(verification["verification_method"], str):
        errors.extend(_error("verification.verification_method must be a string", tag))

    return errors


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def validate_file(path: Path, *, quiet: bool = False) -> int:
    """Validate a single receipt file. Returns 0 if valid, 1 if invalid."""
    tag = path.name

    if not quiet:
        print(f"\n{'=' * 72}")
        print(f"File: {path}")
        print(f"{'=' * 72}")

    if not path.exists():
        if not quiet:
            print(_fail(f"File not found: {path}"))
        return 1

    try:
        content = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        if not quiet:
            print(_fail(f"Invalid JSON: {e}"))
        return 1

    errors = validate_receipt(content, tag=tag)

    if errors:
        for err in errors:
            if not quiet:
                print(err)
        if not quiet:
            print(f"\nValidation failed: {len(errors)} error(s).")
        return 1
    else:
        if not quiet:
            print(_ok("All validations passed."))
        return 0


def main() -> int:
    """Main entry point.

    Without arguments, validates example receipts. With --receipt <path>,
    validates a specific file. With --all, validates all example receipts.
    """
    args = sys.argv[1:]

    if "--receipt" in args:
        idx = args.index("--receipt") + 1
        if idx >= len(args):
            print("error: --receipt requires a file path argument")
            return 1
        path = Path(args[idx])
        return validate_file(path)

    if "--all" in args or not args:
        # Validate all JSON example receipts
        if not DOCS_EXAMPLES_DIR.exists():
            print(f"FAIL: Example directory not found: {DOCS_EXAMPLES_DIR}")
            return 1

        example_files = sorted(DOCS_EXAMPLES_DIR.glob("denops-receipt-*.json"))
        if not example_files:
            print(f"FAIL: No example receipt files found in {DOCS_EXAMPLES_DIR}")
            return 1

        exit_code = 0
        for example_file in example_files:
            result = validate_file(example_file, quiet=False)
            if result != 0:
                exit_code = result

        grand_total = len(example_files)
        failed = exit_code  # actual count of failed files

        print(f"\n{'=' * 72}")
        print(f"Summary: {grand_total} file(s) validated, "
              f"{grand_total - failed} passed, {failed} failed.")
        print(f"{'=' * 72}")
        return exit_code

    # Unknown args
    print(f"Usage: python scripts/validate_denops_receipt.py [--receipt <path> | --all]")
    print("  (no args): validates all example receipts under docs/examples/")
    return 1


if __name__ == "__main__":
    sys.exit(main())

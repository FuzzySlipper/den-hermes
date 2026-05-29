"""Comprehensive tests for DenOps receipt schema validation.

Covers:
- Valid completed receipt passes.
- Valid blocked receipt passes.
- Missing required fields (status, handles, blockers, etc.) fail closed.
- Wrong status value fails.
- Empty main-level handles (no tasks, messages, etc.) allowed for blocked.
- Wrong types fail (string instead of integer).
- Malformed JSON fails.
- Consistency checks (completed + non-empty blockers).
- Boundary checks (summary length).
"""
from __future__ import annotations

import json

import pytest

from scripts.validate_denops_receipt import validate_receipt

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_completed(**overrides):
    """Produce a valid completed receipt dict."""
    receipt = {
        "receipt_version": "1.0",
        "status": "completed",
        "summary": "Verified task #1726 state.",
        "handles": {
            "tasks": [
                {
                    "task_id": 1726,
                    "project_id": "den-hermes-bridge",
                    "status": "in_progress",
                    "message_count": 3,
                    "latest_message_id": 8650,
                }
            ],
            "messages": [
                {
                    "message_id": 8648,
                    "type": "coder_context_packet",
                    "summary": "Launch packet",
                }
            ],
            "documents": [],
            "wake_events": [],
            "delivery_requests": [],
        },
        "blockers": [],
        "assumptions": [
            {
                "assumption": "Task exists",
                "verified": True,
            }
        ],
        "next_required_action": "Runner reviews receipt.",
        "verification": {
            "readback_messages_checked": True,
            "task_thread_readback": True,
            "handle_integrity": True,
            "verification_method": "den_mcp_read_task",
        },
    }
    receipt.update(overrides)
    return receipt


def _make_blocked(**overrides):
    """Produce a valid blocked receipt dict."""
    receipt = {
        "receipt_version": "1.0",
        "status": "blocked",
        "summary": "Cannot verify task state.",
        "handles": {
            "tasks": [],
            "messages": [],
            "documents": [],
            "wake_events": [],
            "delivery_requests": [],
        },
        "blockers": [
            {
                "blocker_summary": "Task not found",
                "blocker_category": "unexpected_state",
                "evidence_handles": ["Den returned empty"],
                "recovery_guidance": "Verify task ID.",
            }
        ],
        "assumptions": [],
        "next_required_action": "Runner verifies task exists.",
        "verification": {
            "readback_messages_checked": False,
            "task_thread_readback": False,
            "handle_integrity": False,
        },
    }
    receipt.update(overrides)
    return receipt


# ---------------------------------------------------------------------------
# Valid receipts
# ---------------------------------------------------------------------------


class TestValidReceipts:
    def test_completed_passes(self):
        errors = validate_receipt(_make_completed())
        assert errors == []

    def test_blocked_passes(self):
        errors = validate_receipt(_make_blocked())
        assert errors == []

    def test_partial_with_empty_blockers(self):
        receipt = _make_completed(status="partial")
        errors = validate_receipt(receipt)
        assert errors == []

    def test_failed_with_blocker(self):
        receipt = _make_blocked(status="failed")
        errors = validate_receipt(receipt)
        assert errors == []

    def test_multi_handle_receipt(self):
        receipt = _make_completed()
        receipt["handles"]["tasks"].append({
            "task_id": 1685,
            "project_id": "den-core",
            "status": "completed",
        })
        receipt["handles"]["documents"].append({
            "document_id": "plan-1685",
            "project_id": "den-core",
            "summary": "Implementation plan",
            "status": "active",
        })
        errors = validate_receipt(receipt)
        assert errors == []


# ---------------------------------------------------------------------------
# Missing required top-level fields
# ---------------------------------------------------------------------------


class TestMissingFields:
    @pytest.mark.parametrize("field", [
        "receipt_version",
        "status",
        "summary",
        "handles",
        "blockers",
        "assumptions",
        "next_required_action",
        "verification",
    ])
    def test_missing_top_level_field(self, field):
        receipt = _make_completed()
        del receipt[field]
        errors = validate_receipt(receipt)
        assert errors, f"Expected error(s) for missing field: {field}"

    def test_missing_handles_task_fields(self):
        receipt = _make_completed()
        del receipt["handles"]["tasks"][0]["task_id"]
        errors = validate_receipt(receipt)
        assert errors

    def test_missing_handles_message_fields(self):
        receipt = _make_completed()
        receipt["handles"]["messages"].append({
            "message_id": 8700,
            # missing "type" and "summary"
        })
        errors = validate_receipt(receipt)
        assert errors

    def test_missing_blocker_fields(self):
        receipt = _make_blocked()
        del receipt["blockers"][0]["blocker_summary"]
        errors = validate_receipt(receipt)
        assert errors

    def test_missing_assumption_fields(self):
        receipt = _make_completed()
        del receipt["assumptions"][0]["assumption"]
        errors = validate_receipt(receipt)
        assert errors

    def test_missing_verification_fields(self):
        receipt = _make_completed()
        del receipt["verification"]["task_thread_readback"]
        errors = validate_receipt(receipt)
        assert errors


# ---------------------------------------------------------------------------
# Invalid values
# ---------------------------------------------------------------------------


class TestInvalidValues:
    def test_invalid_status(self):
        errors = validate_receipt(_make_completed(status="in_progress"))
        assert errors
        assert any("status" in e for e in errors)

    def test_invalid_blocker_category(self):
        receipt = _make_blocked()
        receipt["blockers"][0]["blocker_category"] = "unknown_category"
        errors = validate_receipt(receipt)
        assert errors
        assert any("blocker_category" in e for e in errors)

    def test_invalid_receipt_version(self):
        errors = validate_receipt(_make_completed(receipt_version="0.9"))
        assert errors
        assert any("receipt_version" in e for e in errors)

    def test_wake_event_invalid_status(self):
        receipt = _make_completed()
        receipt["handles"]["wake_events"].append({
            "event_id": "evt-001",
            "summary": "Wake test",
            "status": "cancelled",  # invalid
        })
        errors = validate_receipt(receipt)
        assert errors
        assert any("status" in e for e in errors)

    def test_delivery_request_invalid_status(self):
        receipt = _make_completed()
        receipt["handles"]["delivery_requests"].append({
            "request_id": "req-001",
            "target_identity": "runner",
            "summary": "Deliver receipt",
            "status": "unknown",
        })
        errors = validate_receipt(receipt)
        assert errors

    def test_empty_summary(self):
        errors = validate_receipt(_make_completed(summary=""))
        assert errors
        assert any("summary" in e for e in errors)

    def test_summary_too_long(self):
        errors = validate_receipt(_make_completed(summary="x" * 201))
        assert errors
        assert any("summary" in e and "200" in e for e in errors)


# ---------------------------------------------------------------------------
# Type errors
# ---------------------------------------------------------------------------


class TestTypeErrors:
    def test_task_id_as_string(self):
        receipt = _make_completed()
        receipt["handles"]["tasks"][0]["task_id"] = "1726"
        errors = validate_receipt(receipt)
        assert errors
        assert any("integer" in e.lower() for e in errors)

    def test_message_id_as_string(self):
        receipt = _make_completed()
        receipt["handles"]["messages"][0]["message_id"] = "8648"
        errors = validate_receipt(receipt)
        assert errors

    def test_verification_boolean_as_string(self):
        receipt = _make_completed()
        receipt["verification"]["handle_integrity"] = "true"
        errors = validate_receipt(receipt)
        assert errors
        assert any("boolean" in e.lower() for e in errors)

    def test_assumption_verified_as_string(self):
        receipt = _make_completed()
        receipt["assumptions"][0]["verified"] = "yes"
        errors = validate_receipt(receipt)
        assert errors

    def test_handles_as_array(self):
        receipt = _make_completed()
        receipt["handles"] = []
        errors = validate_receipt(receipt)
        assert errors

    def test_blockers_not_array(self):
        receipt = _make_completed()
        receipt["blockers"] = "none"
        errors = validate_receipt(receipt)
        assert errors


# ---------------------------------------------------------------------------
# Consistency checks
# ---------------------------------------------------------------------------


class TestConsistency:
    def test_completed_with_blockers(self):
        receipt = _make_completed()
        receipt["blockers"] = [
            {
                "blocker_summary": "Something",
                "blocker_category": "unexpected_state",
                "recovery_guidance": "Fix it.",
            }
        ]
        errors = validate_receipt(receipt)
        assert errors
        assert any("blockers" in e for e in errors)

    def test_blocked_with_empty_blockers(self):
        receipt = _make_blocked(blockers=[])
        errors = validate_receipt(receipt)
        assert errors
        assert any("blockers" in e for e in errors)

    def test_partial_with_blockers(self):
        receipt = _make_completed(status="partial")
        receipt["blockers"] = [
            {
                "blocker_summary": "Something",
                "blocker_category": "infrastructure",
                "recovery_guidance": "Fix it.",
            }
        ]
        errors = validate_receipt(receipt)
        assert errors

    def test_failed_with_empty_blockers(self):
        receipt = _make_blocked(status="failed", blockers=[])
        errors = validate_receipt(receipt)
        assert errors


# ---------------------------------------------------------------------------
# Malformed / empty / boundary
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_not_a_dict(self):
        errors = validate_receipt("not a dict")
        assert errors

    def test_empty_dict(self):
        errors = validate_receipt({})
        assert errors

    def test_message_summary_too_long(self):
        receipt = _make_completed()
        receipt["handles"]["messages"][0]["summary"] = "x" * 201
        errors = validate_receipt(receipt)
        assert errors

    def test_empty_handles_arrays_allowed_for_blocked(self):
        """Blocked receipts may have empty handle arrays."""
        receipt = _make_blocked()
        errors = validate_receipt(receipt)
        assert errors == []

    def test_dependencies_integers(self):
        receipt = _make_completed()
        receipt["handles"]["tasks"][0]["dependencies"] = [1684, 1683]
        errors = validate_receipt(receipt)
        assert errors == []

    def test_dependencies_with_non_integer(self):
        receipt = _make_completed()
        receipt["handles"]["tasks"][0]["dependencies"] = [1684, "1683"]
        errors = validate_receipt(receipt)
        assert errors


# ---------------------------------------------------------------------------
# Integration: validate real example files
# ---------------------------------------------------------------------------


class TestExampleFiles:
    def test_completed_example_file(self):
        path = (
            __file__.rsplit("/", 1)[0] if "/" in __file__ else "."
        )
        # Use docs/examples relative to repo root
        import pathlib
        example_dir = (
            pathlib.Path(__file__).resolve().parent.parent
            / "docs" / "examples"
        )
        completed_path = example_dir / "denops-receipt-completed.json"
        content = json.loads(completed_path.read_text())
        errors = validate_receipt(content, tag="completed")
        assert errors == []

    def test_blocked_example_file(self):
        import pathlib
        example_dir = (
            pathlib.Path(__file__).resolve().parent.parent
            / "docs" / "examples"
        )
        blocked_path = example_dir / "denops-receipt-blocked.json"
        content = json.loads(blocked_path.read_text())
        errors = validate_receipt(content, tag="blocked")
        assert errors == []

    def test_multi_handle_example_file(self):
        import pathlib
        example_dir = (
            pathlib.Path(__file__).resolve().parent.parent
            / "docs" / "examples"
        )
        multi_path = example_dir / "denops-receipt-multi-handle.json"
        content = json.loads(multi_path.read_text())
        errors = validate_receipt(content, tag="multi")
        assert errors == []

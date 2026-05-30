"""Tests for the pool-worker assignment smoke helper.

Tests the deterministic PoolWorkerRuntime smoke for all four live roles:
reviewer, validator, drift_checker, packet_auditor.

All tests are fully deterministic: no network I/O, no Den API calls.
"""

from __future__ import annotations

from dataclasses import asdict

import pytest

from scripts.smoke_pool_worker_assignment import (
    ROLE_PACKET_TYPES,
    RoleSmokeResult,
    SmokeReport,
    run_smoke,
    smoke_role,
    format_summary,
)


# ---------------------------------------------------------------------------
# smoke_role — single role
# ---------------------------------------------------------------------------


class TestSmokeRole:
    def test_reviewer_smoke_passes(self):
        result = smoke_role("reviewer")
        assert result.success is True
        assert result.role == "reviewer"
        assert result.pool_member_id == "pool-reviewer-01"
        assert result.initial_state == "pending"
        assert result.acknowledged_state == "acknowledged"
        assert result.ack_checkpoint_type == "assignment_ack"
        assert result.packet_type == "review_findings_packet"
        assert result.capabilities == ["review", "code_audit"]
        assert result.error is None

    def test_validator_smoke_passes(self):
        result = smoke_role("validator")
        assert result.success is True
        assert result.role == "validator"
        assert result.pool_member_id == "pool-validator-01"
        assert result.packet_type == "validation_packet"

    def test_drift_checker_smoke_passes(self):
        result = smoke_role("drift_checker")
        assert result.success is True
        assert result.role == "drift_checker"
        assert result.pool_member_id == "pool-drift-checker-01"
        assert result.packet_type == "drift_check_packet"

    def test_packet_auditor_smoke_passes(self):
        result = smoke_role("packet_auditor")
        assert result.success is True
        assert result.role == "packet_auditor"
        assert result.pool_member_id == "pool-packet-auditor-01"
        assert result.packet_type == "packet_audit_packet"

    def test_unknown_role_returns_error(self):
        result = smoke_role("nonexistent_role")
        assert result.success is False
        assert result.error is not None

    def test_custom_run_id_used(self):
        result = smoke_role("reviewer", run_id="custom-run-id-12345")
        assert result.run_id == "custom-run-id-12345"
        assert result.success is True

    @pytest.mark.parametrize("role", ["reviewer", "validator", "drift_checker", "packet_auditor"])
    def test_all_live_roles_pass(self, role):
        result = smoke_role(role)
        assert result.success is True
        assert result.acknowledged_state == "acknowledged"
        # Verify the pool member prefix matches conventions
        assert result.pool_member_id.startswith("pool-")


# ---------------------------------------------------------------------------
# run_smoke — collective
# ---------------------------------------------------------------------------


class TestRunSmoke:
    def test_all_live_roles(self):
        report = run_smoke()
        assert report.roles_total == 4
        assert report.roles_passed == 4
        assert report.roles_failed == 0
        assert len(report.roles_smoked) == 4
        assert len(report.errors) == 0

    def test_custom_roles(self):
        report = run_smoke(roles=["reviewer", "validator"])
        assert report.roles_total == 2
        assert report.roles_passed == 2
        assert report.roles_failed == 0
        assert [r.role for r in report.roles_smoked] == ["reviewer", "validator"]

    def test_invalid_role_reported(self):
        report = run_smoke(roles=["reviewer", "nope"])
        assert report.roles_total == 2
        assert report.roles_passed == 1
        assert report.roles_failed == 1
        assert len(report.errors) == 1
        assert "nope" in report.errors[0]

    def test_custom_task_id(self):
        report = run_smoke(roles=["reviewer"], task_id=9999)
        result = report.roles_smoked[0]
        assert result.task_id == 9999
        assert f"t9999" in result.run_id


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


class TestFormatSummary:
    def test_format_all_passed(self):
        report = run_smoke()
        summary = format_summary(report)
        assert "PASS" in summary
        assert "pool-reviewer-01" in summary
        assert "pool-validator-01" in summary
        assert "pool-drift-checker-01" in summary
        assert "pool-packet-auditor-01" in summary
        assert "Roles passed: 4" in summary
        assert "Roles failed: 0" in summary

    def test_format_with_errors(self):
        report = run_smoke(roles=["reviewer", "nope"])
        summary = format_summary(report)
        assert "nope" in summary
        assert "GLOBAL ERRORS" in summary

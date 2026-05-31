"""Tests for ``den_hermes.pool_drift`` pool runtime authority drift detection."""

from unittest.mock import MagicMock

import pytest

from den_hermes.pool_drift import (
    ROLE_FROM_PROFILE,
    PoolRuntimeDrift,
    _canonical_role_for_profile,
    check_pool_runtime_drift,
)
from den_hermes.runtime_registry import ResolvedRuntime


def _make_registry_runtime(
    *,
    profile: str = "spawned-coder",
    provider: str = "openai",
    model: str = "gpt-4o",
    role: str = "coder",
) -> ResolvedRuntime:
    return ResolvedRuntime(
        schema_version=1,
        registry_id="test-registry",
        registry_path="/tmp/test-runtimes.yaml",
        registry_fingerprint="sha256:test",
        resolved_at="2026-05-31T00:00:00Z",
        role=role,
        runtime_id="rt-coder-001",
        substrate="spawned_hermes",
        hermes_binary="/usr/local/bin/hermes",
        profile=profile,
        provider=provider,
        model=model,
        toolsets=("terminal", "file"),
        timeout_seconds=300,
        workdir="/tmp/work",
        run_root="/tmp/runs",
        artifact_filename="completion.json",
        log_filename="worker.log",
        source="den-worker",
        extra_args=(),
        preflight={},
    )


# ---------------------------------------------------------------------------
# _canonical_role_for_profile
# ---------------------------------------------------------------------------


class TestCanonicalRoleForProfile:
    def test_spawned_coder_maps_to_coder(self):
        assert _canonical_role_for_profile("spawned-coder") == "coder"

    def test_spawned_reviewer_maps_to_reviewer(self):
        assert _canonical_role_for_profile("spawned-reviewer") == "reviewer"

    def test_spawned_validator_maps_to_validator(self):
        assert _canonical_role_for_profile("spawned-validator") == "validator"

    def test_spawned_drift_checker_maps_to_drift_checker(self):
        assert _canonical_role_for_profile("spawned-drift-checker") == "drift_checker"

    def test_spawned_packet_auditor_maps_to_packet_auditor(self):
        assert _canonical_role_for_profile("spawned-packet-auditor") == "packet_auditor"

    def test_unknown_profile_returns_none(self):
        assert _canonical_role_for_profile("unknown-profile") is None
        assert _canonical_role_for_profile("") is None


# ---------------------------------------------------------------------------
# PoolRuntimeDrift factory methods
# ---------------------------------------------------------------------------


class TestPoolRuntimeDrift:
    def test_no_drift(self):
        d = PoolRuntimeDrift.no_drift()
        assert d.drifted is False
        assert d.details == "No drift detected."
        assert d.registry_mismatch is False

    def test_missing_pool_identity_both_empty(self):
        d = PoolRuntimeDrift.missing_pool_identity(pool_member_id="", pool_profile="")
        assert d.drifted is True
        assert "DEN_HERMES_POOL_MEMBER_ID" in d.details
        assert "DEN_HERMES_PROFILE" in d.details

    def test_missing_pool_identity_only_member(self):
        d = PoolRuntimeDrift.missing_pool_identity(pool_member_id=None, pool_profile="spawned-coder")
        assert d.drifted is True
        assert "DEN_HERMES_POOL_MEMBER_ID" in d.details
        assert "DEN_HERMES_PROFILE" not in d.details

    def test_missing_pool_identity_only_profile(self):
        d = PoolRuntimeDrift.missing_pool_identity(pool_member_id="pool-coder-01", pool_profile=None)
        assert d.drifted is True
        assert "DEN_HERMES_PROFILE" in d.details

    def test_role_profile_mismatch(self):
        d = PoolRuntimeDrift.role_profile_mismatch(
            role="coder",
            pool_profile="spawned-reviewer",
            expected_role="reviewer",
        )
        assert d.drifted is True
        assert "spawned-reviewer" in d.details
        assert "coder" in d.details

    def test_registry_mismatch_only(self):
        d = PoolRuntimeDrift.registry_mismatch_only(
            registry_provider="openai",
            registry_model="gpt-4o",
            registry_profile="spawned-coder",
            pool_profile="spawned-coder",
            pool_member_id="pool-coder-01",
        )
        assert d.drifted is False  # informational only
        assert d.registry_mismatch is True
        assert "pool-coder-01" in d.details or "pool_member_id" in str(d.diagnostics)


# ---------------------------------------------------------------------------
# check_pool_runtime_drift integration
# ---------------------------------------------------------------------------


class TestCheckPoolRuntimeDrift:
    def test_no_drift_when_all_evidence_present(self):
        runtime = _make_registry_runtime(profile="spawned-coder")
        pool_member_id = "pool-coder-01"
        pool_profile = "spawned-coder"
        result = check_pool_runtime_drift(
            registry_runtime=runtime,
            role="coder",
            pool_member_id=pool_member_id,
            pool_profile=pool_profile,
        )
        assert result.drifted is False

    def test_blocking_missing_pool_member_id(self):
        runtime = _make_registry_runtime(profile="spawned-coder")
        result = check_pool_runtime_drift(
            registry_runtime=runtime,
            role="coder",
            pool_member_id=None,
            pool_profile="spawned-coder",
        )
        assert result.drifted is True
        assert "DEN_HERMES_POOL_MEMBER_ID" in result.details

    def test_blocking_missing_pool_profile(self):
        runtime = _make_registry_runtime(profile="spawned-coder")
        result = check_pool_runtime_drift(
            registry_runtime=runtime,
            role="coder",
            pool_member_id="pool-coder-01",
            pool_profile=None,
        )
        assert result.drifted is True
        assert "DEN_HERMES_PROFILE" in result.details

    def test_blocking_role_profile_mismatch(self):
        runtime = _make_registry_runtime(profile="spawned-coder")
        result = check_pool_runtime_drift(
            registry_runtime=runtime,
            role="coder",
            pool_member_id="pool-coder-01",
            pool_profile="spawned-reviewer",  # wrong profile!
        )
        assert result.drifted is True
        assert "spawned-reviewer" in result.details
        assert "coder" in result.details

    def test_registry_provider_mismatch_is_not_blocking(self):
        """Correction #1 from #9562: registry provider/model mismatch is informational."""
        runtime = _make_registry_runtime(
            profile="spawned-coder",
            provider="anthropic",
            model="claude-sonnet-4",
        )
        result = check_pool_runtime_drift(
            registry_runtime=runtime,
            role="coder",
            pool_member_id="pool-coder-01",
            pool_profile="spawned-coder",
        )
        assert result.drifted is False  # NOT blocking

    def test_registry_mismatch_is_non_blocking_informational(self):
        """Correction #2 from #9562: registry != profile is informational, not blocking."""
        runtime = _make_registry_runtime(
            profile="different-profile",
            provider="anthropic",
            model="claude-sonnet-4",
        )
        result = check_pool_runtime_drift(
            registry_runtime=runtime,
            role="coder",
            pool_member_id="pool-coder-01",
            pool_profile="spawned-coder",
        )
        # Registry profile differs from pool profile, but this is informational
        assert result.drifted is False
        assert result.registry_mismatch is True

    def test_empty_pool_member_id_is_blocking(self):
        runtime = _make_registry_runtime(profile="spawned-coder")
        result = check_pool_runtime_drift(
            registry_runtime=runtime,
            role="coder",
            pool_member_id="",
            pool_profile="spawned-coder",
        )
        assert result.drifted is True

    def test_empty_pool_profile_is_blocking(self):
        runtime = _make_registry_runtime(profile="spawned-coder")
        result = check_pool_runtime_drift(
            registry_runtime=runtime,
            role="coder",
            pool_member_id="pool-coder-01",
            pool_profile="",
        )
        assert result.drifted is True

    def test_role_profile_mismatch_unknown_profile(self):
        """Unknown profile names return None from _canonical_role_for_profile."""
        runtime = _make_registry_runtime(profile="spawned-coder")
        result = check_pool_runtime_drift(
            registry_runtime=runtime,
            role="coder",
            pool_member_id="pool-coder-01",
            pool_profile="unknown-profile",
        )
        # Unknown profile → _canonical_role_for_profile returns None,
        # which is not a mismatch failure
        assert result.drifted is False

    def test_role_profile_mismatch_for_non_coder_role(self):
        runtime = _make_registry_runtime(profile="spawned-reviewer", role="reviewer")
        result = check_pool_runtime_drift(
            registry_runtime=runtime,
            role="reviewer",
            pool_member_id="pool-reviewer-01",
            pool_profile="spawned-coder",  # wrong! should be spawned-reviewer for role=reviewer
        )
        assert result.drifted is True
        assert "reviewer" in result.details
        assert "spawned-coder" in result.details

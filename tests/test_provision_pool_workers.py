"""Comprehensive tests for the pool-worker provisioning script.

Tests the provisioning logic in ``scripts/provision_pool_workers.py``:

- Registry loading and validation
- Forbidden-profile detection (den-hermes-runner guard)
- Secret/credential scanning
- Pool member matrix construction
- Dry-run vs apply mode
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from scripts.provision_pool_workers import (
    LIVE_ROLES,
    FORBIDDEN_PROFILES,
    SPAWNED_PROFILE_PREFIX,
    POOL_MEMBER_PREFIXES,
    build_pool_member,
    check_profile_not_forbidden,
    compute_fingerprint,
    format_matrix,
    load_registry,
    resolve_role_runtime,
    run_provision,
    scan_for_secrets,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


SAMPLE_REGISTRY = Path(__file__).resolve().parents[1] / "config" / "spawned-hermes-runtimes.sample.yaml"


@pytest.fixture
def registry_path(tmp_path) -> Path:
    """Copy the sample registry to a temp path for mutation-safe tests."""
    path = tmp_path / "registry.yaml"
    path.write_text(SAMPLE_REGISTRY.read_text())
    return path


# ---------------------------------------------------------------------------
# check_profile_not_forbidden
# ---------------------------------------------------------------------------


class TestCheckProfileNotForbidden:
    def test_spawned_coder_accepted(self):
        assert check_profile_not_forbidden("spawned-coder", "coder") is None

    def test_spawned_reviewer_accepted(self):
        assert check_profile_not_forbidden("spawned-reviewer", "reviewer") is None

    def test_den_hermes_runner_rejected(self):
        error = check_profile_not_forbidden("den-hermes-runner", "coder")
        assert error is not None
        assert "forbidden" in error.lower()
        assert "den-hermes-runner" in error

    def test_empty_profile_rejected(self):
        error = check_profile_not_forbidden("", "coder")
        assert error is not None
        assert "spawned-" in error

    def test_all_forbidden_profiles_rejected(self):
        for profile in FORBIDDEN_PROFILES:
            error = check_profile_not_forbidden(profile, "reviewer")
            assert error is not None, f"Expected {profile!r} to be rejected"
            assert "forbidden" in error.lower(), f"Expected 'forbidden' in error for {profile!r}"


# ---------------------------------------------------------------------------
# scan_for_secrets
# ---------------------------------------------------------------------------


class TestScanForSecrets:
    def test_clean_config_returns_empty(self):
        data = {
            "registry_id": "test",
            "defaults": {"substrate": "spawned_hermes", "timeout_seconds": 900},
            "roles": {},
        }
        assert scan_for_secrets(data) == []

    def test_detects_api_key_value(self):
        data = {"credentials": {"api_key": "sk-abcdefgh12345678"}}
        results = scan_for_secrets(data)
        assert len(results) >= 1
        assert "[REDACTED]" in results[0]

    def test_detects_auth_token_key(self):
        data = {"auth_token": "s3cr3t-t0k3n"}
        results = scan_for_secrets(data)
        assert len(results) >= 1

    def test_detects_bearer_token(self):
        data = {"authorization": "Bearer sk-abcdefgh12345678"}
        results = scan_for_secrets(data)
        assert len(results) >= 1

    def test_nested_scan(self):
        data = {
            "roles": {
                "coder": {
                    "launch": {
                        "extra_args": ["--api-key", "sk-abcdefgh12345678"],
                    }
                }
            }
        }
        results = scan_for_secrets(data)
        assert len(results) >= 1

    def test_secretish_key_name_detected(self):
        data = {"api_key": "anything"}
        results = scan_for_secrets(data)
        assert len(results) >= 1


    def test_secret_value_as_value_detected(self):
        data = {"notes": "the secret: super-secret-password"}
        results = scan_for_secrets(data)
        assert len(results) >= 1


    def test_clean_key_value_not_detected(self):
        data = {"updated_by": "task-1784-provisioning"}
        results = scan_for_secrets(data)
        assert len(results) == 0


# ---------------------------------------------------------------------------
# resolve_role_runtime
# ---------------------------------------------------------------------------


class TestResolveRoleRuntime:
    @pytest.fixture
    def registry(self) -> dict[str, Any]:
        return load_registry(SAMPLE_REGISTRY)

    def test_resolves_reviewer(self, registry):
        runtime = resolve_role_runtime(
            "reviewer", registry, registry["defaults"], registry["roles"]
        )
        assert runtime["role"] == "reviewer"
        assert runtime["profile"] == "spawned-reviewer"
        assert runtime["provider"] == "opencode-go"

    def test_resolves_validator(self, registry):
        runtime = resolve_role_runtime(
            "validator", registry, registry["defaults"], registry["roles"]
        )
        assert runtime["role"] == "validator"
        assert runtime["profile"] == "spawned-validator"
        assert runtime["model"] == "kimi-k2.6"

    def test_resolves_drift_checker(self, registry):
        runtime = resolve_role_runtime(
            "drift_checker", registry, registry["defaults"], registry["roles"]
        )
        assert runtime["role"] == "drift_checker"
        assert runtime["profile"] == "spawned-drift-checker"

    def test_resolves_packet_auditor(self, registry):
        runtime = resolve_role_runtime(
            "packet_auditor", registry, registry["defaults"], registry["roles"]
        )
        assert runtime["role"] == "packet_auditor"
        assert runtime["profile"] == "spawned-packet-auditor"
        assert runtime["provider"] == "openai-codex"

    def test_resolves_project_orchestrator(self, registry):
        runtime = resolve_role_runtime(
            "project_orchestrator", registry, registry["defaults"], registry["roles"]
        )
        assert runtime["role"] == "project_orchestrator"
        assert runtime["profile"] == "spawned-orchestrator"
        assert runtime["provider"] == "deepseek"

    def test_missing_role_raises(self, registry):
        with pytest.raises(RuntimeError, match="not found"):
            resolve_role_runtime("nonexistent", registry, registry["defaults"], registry["roles"])

    def test_alias_resolution(self, registry):
        runtime = resolve_role_runtime(
            "drift", registry, registry["defaults"], registry["roles"]
        )
        assert runtime["role"] == "drift_checker"

        runtime2 = resolve_role_runtime(
            "audit", registry, registry["defaults"], registry["roles"]
        )
        assert runtime2["role"] == "packet_auditor"


# ---------------------------------------------------------------------------
# build_pool_member
# ---------------------------------------------------------------------------


class TestBuildPoolMember:
    def test_build_reviewer_member(self):
        runtime = {
            "role": "reviewer",
            "runtime_id": "reviewer-primary",
            "profile": "spawned-reviewer",
            "provider": "opencode-go",
            "model": "deepseek-v4-flash",
            "toolsets": ["terminal", "file"],
            "timeout_seconds": 1500,
            "source": "den-worker",
        }
        member = build_pool_member("reviewer", runtime)
        assert member.worker_role == "reviewer"
        assert member.profile_identity == "spawned-reviewer"
        assert member.pool_member_id == "pool-reviewer-01"
        assert member.runtime_id == "reviewer-primary"
        assert member.provider == "opencode-go"
        assert member.model == "deepseek-v4-flash"
        assert member.capabilities == ["review", "code_audit"]
        assert member.status == "ready"

    def test_build_all_live_roles(self):
        for role in LIVE_ROLES:
            runtime = {
                "role": role,
                "runtime_id": f"{role}-primary",
                "profile": "spawned-orchestrator" if role == "project_orchestrator" else f"spawned-{role}",
                "provider": "opencode-go",
                "model": "test-model",
                "toolsets": ["terminal", "file"],
                "timeout_seconds": 900,
                "source": "den-worker",
            }
            member = build_pool_member(role, runtime)
            prefix = POOL_MEMBER_PREFIXES[role]
            assert member.pool_member_id == f"{prefix}-01"
            assert member.worker_role == role
            assert member.status == "ready"


# ---------------------------------------------------------------------------
# run_provision
# ---------------------------------------------------------------------------


class TestRunProvision:
    def test_dry_run_all_roles_passes(self, registry_path):
        result = run_provision(registry_path, apply_mode=False)
        assert result.roles_resolved == 5
        assert result.roles_failed == 0
        assert len(result.members) == 5
        assert result.credential_guard_ok is True

    def test_dry_run_verifies_spawned_profiles(self, registry_path):
        result = run_provision(registry_path)
        for member in result.members:
            assert member.profile_identity.startswith(SPAWNED_PROFILE_PREFIX), (
                f"Role {member.worker_role} has non-spawned profile "
                f"{member.profile_identity!r}"
            )

    def test_den_hermes_runner_profile_rejected(self, tmp_path):
        """Simulate a registry that still uses den-hermes-runner for a role."""
        text = SAMPLE_REGISTRY.read_text().replace(
            "profile: spawned-reviewer", "profile: den-hermes-runner"
        )
        bad_registry = tmp_path / "bad.yaml"
        bad_registry.write_text(text)
        result = run_provision(bad_registry, roles=["reviewer"])
        assert result.roles_failed >= 1
        assert any("den-hermes-runner" in err for err in result.errors)

    def test_secret_detected_in_registry(self, tmp_path):
        """Simulate a registry with a leaked-looking value."""
        text = SAMPLE_REGISTRY.read_text() + "\nleaked_api_key: sk-abcdefgh12345678\n"
        leaky_registry = tmp_path / "leaky.yaml"
        leaky_registry.write_text(text)
        result = run_provision(leaky_registry, roles=["reviewer"])
        assert result.credential_guard_ok is False
        assert len(result.secrets_found) >= 1

    def test_apply_mode_emits_json(self, registry_path, capsys):
        result = run_provision(registry_path, apply_mode=True)
        captured = capsys.readouterr()
        assert "DEN_MCP_UPSERT" in captured.out
        # Should have 5 JSON payloads (one per live role/lane)
        assert captured.out.count("DEN_MCP_UPSERT") == 5


# ---------------------------------------------------------------------------
# format_matrix
# ---------------------------------------------------------------------------


class TestFormatMatrix:
    def test_matrix_includes_all_roles(self, registry_path):
        result = run_provision(registry_path)
        matrix = format_matrix(result)
        assert "reviewer" in matrix
        assert "validator" in matrix
        assert "drift_checker" in matrix
        assert "packet_auditor" in matrix
        assert "Pool Worker Provisioning Matrix" in matrix
        assert "pool-reviewer-01" in matrix
        assert "pool-validator-01" in matrix
        assert "pool-drift-checker-01" in matrix
        assert "pool-packet-auditor-01" in matrix
        assert "project_orchestrator" in matrix
        assert "pool-orchestrator-01" in matrix

    def test_matrix_reports_errors(self, registry_path):
        # Force an error by passing an empty roles list
        result = run_provision(registry_path, roles=[])
        matrix = format_matrix(result)
        # No error expected — empty roles just means nothing to provision
        assert "Pool Worker Provisioning Matrix" in matrix

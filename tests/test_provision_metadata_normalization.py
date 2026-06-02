"""Tests for provisioning metadata normalization (task #1836).

Verifies that pool-member metadata uses a ``provisioning`` object
for historical fields instead of bare ``task_id``/``repo``/``source``
at the top level.
"""
import io
import json
import sys

import pytest

# Ensure scripts directory is importable
sys.path.insert(0, "scripts")

from provision_pool_workers import (
    ResolvedPoolMember,
    ProvisioningResult,
    _emit_apply_payloads,
    build_pool_member,
)
from smoke_pool_worker_assignment import main, smoke_role, RoleSmokeResult


# ---------------------------------------------------------------------------
# ResolvedPoolMember invariants
# ---------------------------------------------------------------------------


def test_resolved_pool_member_has_no_top_level_task_id_repo_source():
    """ResolvedPoolMember must NOT have bare task_id/repo/source at top level."""
    member = ResolvedPoolMember(
        worker_role="coder",
        profile_identity="spawned-coder",
        worker_identity="pool-coder-01",
        agent_instance_id_template="hermes:den-k8:spawned-coder:pool-coder-01:{id}",
        pool_member_id="pool-coder-01",
        runtime_id="coder-primary",
        provider="openrouter",
        model="model-coder",
        capabilities=["code"],
        timeout_seconds=600,
        provisioning_source="reviewed_provisioning_script",
    )

    d = member.__dict__
    assert not hasattr(member, "task_id"), "ResolvedPoolMember must not have task_id"
    assert not hasattr(member, "repo"), "ResolvedPoolMember must not have repo"
    assert not hasattr(member, "source"), "ResolvedPoolMember must not have source"
    assert member.provisioning_source == "reviewed_provisioning_script"


def test_resolved_pool_member_provisioning_fields_exist():
    """ResolvedPoolMember must have provisioning_source, provisioning_repo."""
    member = ResolvedPoolMember(
        worker_role="reviewer",
        profile_identity="spawned-reviewer",
        worker_identity="pool-reviewer-01",
        agent_instance_id_template="x",
        pool_member_id="pool-reviewer-01",
        runtime_id="rev",
        provider="p",
        model="m",
        capabilities=[],
        timeout_seconds=300,
        provisioning_source="task1812_prep",
        provisioning_repo="den-hermes",
    )

    assert member.provisioning_source == "task1812_prep"
    assert member.provisioning_repo == "den-hermes"


# ---------------------------------------------------------------------------
# build_pool_member invariants
# ---------------------------------------------------------------------------


def test_build_pool_member_populates_provisioning_source():
    """build_pool_member carries provisioning_source from runtime."""
    runtime = {
        "profile": "spawned-coder",
        "runtime_id": "coder-primary",
        "provider": "p",
        "model": "m",
        "provisioning_source": "provisioning_script",
        "toolsets": ["terminal", "file"],
        "timeout_seconds": 600,
    }
    member = build_pool_member("coder", runtime)

    assert member.provisioning_source == "provisioning_script"
    assert member.worker_role == "coder"
    assert member.pool_member_id == "pool-coder-01"


# ---------------------------------------------------------------------------
# _emit_apply_payloads invariants
# ---------------------------------------------------------------------------


def test_emit_apply_payloads_has_provisioning_object():
    """_emit_apply_payloads outputs a provisioning object, not bare task_id/repo."""
    member = ResolvedPoolMember(
        worker_role="coder",
        profile_identity="spawned-coder",
        worker_identity="pool-coder-01",
        agent_instance_id_template="x",
        pool_member_id="pool-coder-01",
        runtime_id="c",
        provider="p",
        model="m",
        capabilities=["code"],
        timeout_seconds=600,
        provisioning_source="reviewed_script",
        provisioning_repo="den-hermes",
    )
    result = ProvisioningResult(
        registry_path="/fake/registry.yaml",
        registry_id="test-registry",
        registry_fingerprint="abc123",
        roles_resolved=1,
        members=[member],
    )

    captured = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = captured
    try:
        _emit_apply_payloads(result)
    finally:
        sys.stdout = old_stdout

    output = captured.getvalue()
    parsed = json.loads(output[output.index("{") : output.rindex("}") + 1])

    p = parsed["payload"]
    assert "provisioning" in p
    assert p["provisioning"]["source"] == "reviewed_script"
    assert p["provisioning"]["repo"] == "den-hermes"
    assert p["provisioning"]["registry_fingerprint"] == "abc123"
    # Must NOT have bare task_id/repo/source at top level
    assert "task_id" not in p
    assert "repo" not in p
    assert "source" not in p


# ---------------------------------------------------------------------------
# smoke_role metadata invariants
# ---------------------------------------------------------------------------


def test_smoke_role_metadata_nests_under_provisioning():
    """smoke_role assignment metadata nests under provisioning key."""
    result = smoke_role("coder", task_id=1836, project_id="den-hermes-bridge")

    assert isinstance(result, RoleSmokeResult)
    assert result.task_id == 1836
    assert result.project_id == "den-hermes-bridge"
    assert result.assignment_metadata["smoke"] is True
    assert result.assignment_metadata["provisioning"] == {
        "source": "smoke_pool_worker_assignment.py",
        "task_id": 1836,
    }
    assert "task_id" not in {
        key: value
        for key, value in result.assignment_metadata.items()
        if key != "provisioning"
    }


def test_smoke_role_uses_cli_task_id():
    """smoke_role with explicit task_id uses that value."""
    result = smoke_role("reviewer", task_id=9999, project_id="den-core")
    assert result.task_id == 9999
    assert result.project_id == "den-core"
    assert result.assignment_metadata["provisioning"]["task_id"] == 9999


def test_smoke_cli_accepts_task_and_project_args(capsys):
    """CLI exposes task/project args and propagates them into JSON output."""
    exit_code = main([
        "--roles",
        "validator",
        "--task-id",
        "1836",
        "--project-id",
        "den-core",
        "--json",
    ])
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)

    assert exit_code == 0
    assert parsed["roles_passed"] == 1
    result = parsed["results"][0]
    assert result["task_id"] == 1836
    assert result["project_id"] == "den-core"
    assert result["assignment_metadata"]["provisioning"] == {
        "source": "smoke_pool_worker_assignment.py",
        "task_id": 1836,
    }

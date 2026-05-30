from pathlib import Path

from den_hermes.runtime_ops import (
    format_runtime_matrix,
    preflight_runtime_roles,
    validate_runtime_registry,
)


SAMPLE_REGISTRY = Path(__file__).resolve().parents[1] / "config" / "spawned-hermes-runtimes.sample.yaml"


def test_format_runtime_matrix_shows_active_role_picks():
    output = format_runtime_matrix(SAMPLE_REGISTRY)

    assert "Registry: den-hermes-runner-defaults" in output
    assert "ROLE" in output
    assert "coder" in output
    assert "reviewer" in output
    assert "spawned-coder" in output
    assert "opencode-go" in output
    assert "glm-5.1" in output
    assert "terminal,file" in output


def test_validate_runtime_registry_reports_all_required_roles():
    result = validate_runtime_registry(SAMPLE_REGISTRY)

    assert result["ok"] is True
    assert result["roles"] == ["coder", "reviewer", "validator", "drift_checker", "packet_auditor"]
    assert result["registry_id"] == "den-hermes-runner-defaults"


def test_preflight_runtime_roles_runs_exact_resolved_commands():
    calls = []

    def fake_runner(command, timeout_seconds):
        calls.append((command, timeout_seconds))
        return {"exit_code": 0, "stdout": "PROFILE_OK\n", "stderr": ""}

    results = preflight_runtime_roles(SAMPLE_REGISTRY, roles=["coder", "reviewer"], runner=fake_runner)

    assert [result["role"] for result in results] == ["coder", "reviewer"]
    assert all(result["ok"] for result in results)
    assert calls[0][0][0:3] == ["hermes", "--profile", "spawned-coder"]
    assert calls[0][0][calls[0][0].index("--provider") + 1] == "opencode-go"
    assert calls[0][0][calls[0][0].index("--model") + 1] == "glm-5.1"
    assert calls[0][1] == 300


def test_preflight_runtime_roles_redacts_command_failures():
    def fake_runner(command, timeout_seconds):
        return {"exit_code": 1, "stdout": "", "stderr": "provider key sk-this-is-secret-looking failed"}

    results = preflight_runtime_roles(SAMPLE_REGISTRY, roles=["coder"], runner=fake_runner)

    assert results[0]["ok"] is False
    assert "[REDACTED]" in results[0]["stderr"]
    assert "sk-this-is-secret-looking" not in results[0]["stderr"]

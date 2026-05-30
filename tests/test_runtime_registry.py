from pathlib import Path

import pytest

from den_hermes.runtime_registry import (
    DEFAULT_RUNTIME_REGISTRY_PATH,
    RuntimeRegistryError,
    resolve_role_runtime,
)


SAMPLE_REGISTRY = Path(__file__).resolve().parents[1] / "config" / "spawned-hermes-runtimes.sample.yaml"


def test_default_runtime_registry_path_is_operator_central():
    assert DEFAULT_RUNTIME_REGISTRY_PATH == Path("/home/agents/runtime/spawned-hermes-runtimes.yaml")


def test_resolver_loads_required_roles_from_sample_config():
    runtime = resolve_role_runtime("coder", registry_path=SAMPLE_REGISTRY)

    assert runtime.role == "coder"
    assert runtime.runtime_id == "coder-primary"
    assert runtime.profile == "spawned-coder"
    assert runtime.provider == "opencode-go"
    assert runtime.model == "glm-5.1"
    assert runtime.toolsets == ("terminal", "file")
    assert runtime.timeout_seconds == 1800
    assert runtime.registry_id == "den-hermes-runner-defaults"
    assert runtime.registry_fingerprint.startswith("sha256:")


def test_resolver_uses_central_registry_env_path(monkeypatch, tmp_path):
    registry = tmp_path / "central.yaml"
    registry.write_text(SAMPLE_REGISTRY.read_text().replace("model: glm-5.1", "model: gpt-4.1", 1))
    monkeypatch.setenv("DEN_HERMES_RUNTIME_REGISTRY", str(registry))

    runtime = resolve_role_runtime("coder")

    assert runtime.registry_path == str(registry)
    assert runtime.model == "gpt-4.1"


def test_changing_central_config_changes_role_resolution(tmp_path):
    registry = tmp_path / "central.yaml"
    registry.write_text(SAMPLE_REGISTRY.read_text())
    before = resolve_role_runtime("reviewer", registry_path=registry)

    registry.write_text(SAMPLE_REGISTRY.read_text().replace("model: deepseek-v4-flash", "model: gpt-4.1", 1))
    after = resolve_role_runtime("reviewer", registry_path=registry)

    assert before.model == "deepseek-v4-flash"
    assert after.model == "gpt-4.1"
    assert before.registry_fingerprint != after.registry_fingerprint


def test_resolver_applies_central_defaults_and_computes_paths(tmp_path):
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        """
schema_version: 1
registry_id: test-registry
defaults:
  substrate: spawned_hermes
  hermes_binary: hermes
  run_root: {run_root}
  artifact_filename: completion.json
  log_filename: worker.log
  profile_required: true
  provider_required: true
  model_required: true
  timeout_seconds: 777
  toolsets: [file]
  workdir: {workdir}
roles:
  coder:
    runtime_id: coder-test
    profile: den-hermes-runner
    provider: openai-codex
    model: gpt-5.5
  reviewer:
    runtime_id: reviewer-test
    profile: den-hermes-runner
    provider: openai-codex
    model: gpt-5.5
  validator:
    runtime_id: validator-test
    profile: den-hermes-runner
    provider: openai-codex
    model: gpt-5.5
  drift_checker:
    runtime_id: drift-test
    profile: den-hermes-runner
    provider: openai-codex
    model: gpt-5.5
  packet_auditor:
    runtime_id: audit-test
    profile: den-hermes-runner
    provider: openai-codex
    model: gpt-5.5
""".format(run_root=tmp_path / "runs", workdir=tmp_path)
    )

    runtime = resolve_role_runtime("coder", registry_path=registry, run_id="run-123")

    assert runtime.toolsets == ("file",)
    assert runtime.timeout_seconds == 777
    assert runtime.artifact_path == str(tmp_path / "runs" / "run-123" / "completion.json")
    assert runtime.log_path == str(tmp_path / "runs" / "run-123" / "worker.log")


def test_resolver_rejects_missing_required_role(tmp_path):
    registry = tmp_path / "registry.yaml"
    text = SAMPLE_REGISTRY.read_text().replace("  reviewer:\n    runtime_id: reviewer-primary", "  review_removed:\n    runtime_id: reviewer-primary")
    registry.write_text(text)

    with pytest.raises(RuntimeRegistryError, match="reviewer"):
        resolve_role_runtime("coder", registry_path=registry)


def test_resolver_rejects_missing_explicit_profile(tmp_path):
    registry = tmp_path / "registry.yaml"
    text = SAMPLE_REGISTRY.read_text().replace("    profile: spawned-coder\n    provider: opencode-go", "    provider: opencode-go", 1)
    registry.write_text(text)

    with pytest.raises(RuntimeRegistryError, match="profile"):
        resolve_role_runtime("coder", registry_path=registry)


def test_resolver_rejects_missing_provider_or_model_by_default(tmp_path):
    registry = tmp_path / "registry.yaml"
    text = SAMPLE_REGISTRY.read_text().replace("    model: glm-5.1\n    toolsets: [terminal, file]", "    toolsets: [terminal, file]", 1)
    registry.write_text(text)

    with pytest.raises(RuntimeRegistryError, match="model"):
        resolve_role_runtime("coder", registry_path=registry)


def test_resolver_rejects_project_local_override_without_escape_hatch():
    with pytest.raises(RuntimeRegistryError, match="allow_runtime_override"):
        resolve_role_runtime(
            "coder",
            registry_path=SAMPLE_REGISTRY,
            overrides={"provider": "openrouter", "model": "anthropic/claude-sonnet-4"},
        )


def test_resolver_allows_audited_emergency_override():
    runtime = resolve_role_runtime(
        "coder",
        registry_path=SAMPLE_REGISTRY,
        allow_runtime_override=True,
        override_reason="primary provider outage",
        requested_by="den-hermes-runner",
        overrides={"provider": "openrouter", "model": "anthropic/claude-sonnet-4"},
    )

    assert runtime.provider == "openrouter"
    assert runtime.model == "anthropic/claude-sonnet-4"
    assert runtime.override == {
        "reason": "primary provider outage",
        "requested_by": "den-hermes-runner",
        "fields": {"provider": "openrouter", "model": "anthropic/claude-sonnet-4"},
    }


def test_resolver_normalizes_aliases_to_canonical_roles():
    runtime = resolve_role_runtime("drift", registry_path=SAMPLE_REGISTRY)

    assert runtime.role == "drift_checker"
    assert runtime.runtime_id == "drift-checker-primary"


def test_resolver_redacts_secret_like_values(tmp_path):
    registry = tmp_path / "registry.yaml"
    text = SAMPLE_REGISTRY.read_text().replace("    provider: opencode-go", "    provider: sk-abcdefghijklmnop", 1)
    registry.write_text(text)

    with pytest.raises(RuntimeRegistryError) as excinfo:
        resolve_role_runtime("coder", registry_path=registry)

    message = str(excinfo.value)
    assert "[REDACTED]" in message
    assert "sk-abc...mnop" not in message


def test_resolved_runtime_serializes_to_den_registration_args(tmp_path):
    runtime = resolve_role_runtime("coder", registry_path=SAMPLE_REGISTRY, run_id="coder-run")

    args = runtime.to_den_registration_args(workdir=str(tmp_path), host="den-k8plus")

    assert args["profile"] == "spawned-coder"
    assert args["provider"] == "opencode-go"
    assert args["model"] == "glm-5.1"
    assert args["toolsets"] == "terminal,file"
    assert args["timeout_seconds"] == 1800
    assert args["artifact_path"].endswith("/coder-run/completion.json")
    assert args["log_path"].endswith("/coder-run/worker.log")
    assert args["workdir"] == str(tmp_path)
    assert args["host"] == "den-k8plus"


def test_preflight_command_uses_exact_resolved_profile_provider_model():
    runtime = resolve_role_runtime("coder", registry_path=SAMPLE_REGISTRY)

    command = runtime.preflight_command()

    assert command[:3] == ["hermes", "--profile", "spawned-coder"]
    assert "chat" in command
    assert command[command.index("--provider") + 1] == "opencode-go"
    assert command[command.index("--model") + 1] == "glm-5.1"
    assert command[command.index("--toolsets") + 1] == ""
    assert command[command.index("--source") + 1] == "den-runtime-preflight"
    assert "PROFILE_OK" in command[command.index("-q") + 1]

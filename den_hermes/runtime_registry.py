from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

import yaml


CANONICAL_ROLES = frozenset({"coder", "reviewer", "validator", "drift_checker", "packet_auditor", "project_orchestrator"})
SECRETISH_PATTERN = re.compile(r"(?i)(sk-[a-z0-9_-]{8,}|api[_-]?key|auth[_-]?token|secret|bearer\s+)")
DEFAULT_PREFLIGHT_PROMPT = "Reply with exactly: PROFILE_OK"
DEFAULT_PREFLIGHT_EXPECTED = "PROFILE_OK"
DEFAULT_RUNTIME_REGISTRY_PATH = Path("/home/agents/runtime/retired/20260608-spawned-hermes-runtimes-yaml/spawned-hermes-runtimes.yaml")


# ── 2026-06-19: RETIRED / LEGACY ──────────────────────────────────
#
# The YAML runtime registry at spawned-hermes-runtimes.yaml has been
# retired.  The live worker-pool runtime authority is:
#
#   Den Core pool members/assignments/leases
#   + Channels direct-agent delivery
#   + concrete Hermes profile/service config
#   + Pi Crew runtime back-end
#
# resolve_role_runtime() now fails closed with a clear message.
# The old implementation is preserved in _legacy_resolve_role_runtime()
# so existing tests can still import and validate the shape.
# ──────────────────────────────────────────────────────────────────


class RuntimeRegistryError(ValueError):
    """Raised when the spawned-Hermes runtime registry cannot be safely resolved."""


def _redact_secretish(value: Any) -> str:
    text = str(value)
    return SECRETISH_PATTERN.sub("[REDACTED]", text)


@dataclass(frozen=True)
class ResolvedRuntime:
    schema_version: int
    registry_id: str
    registry_path: str
    registry_fingerprint: str
    resolved_at: str
    role: str
    runtime_id: str
    substrate: str
    hermes_binary: str
    profile: str
    provider: str
    model: str
    toolsets: tuple[str, ...]
    timeout_seconds: int
    workdir: str
    run_root: str
    artifact_filename: str
    log_filename: str
    source: str
    extra_args: tuple[str, ...]
    preflight: Mapping[str, Any]
    artifact_path: str | None = None
    log_path: str | None = None
    override: Mapping[str, Any] | None = None

    def to_den_registration_args(self, *, workdir: str | None = None, host: str | None = None) -> dict[str, Any]:
        args: dict[str, Any] = {
            "profile": self.profile,
            "provider": self.provider,
            "model": self.model,
            "toolsets": ",".join(self.toolsets),
            "workdir": workdir or self.workdir,
            "timeout_seconds": self.timeout_seconds,
        }
        if self.artifact_path is not None:
            args["artifact_path"] = self.artifact_path
        if self.log_path is not None:
            args["log_path"] = self.log_path
        if host is not None:
            args["host"] = host
        return args

    def audit_metadata(self) -> dict[str, Any]:
        return {
            "runtime_registry": {
                "registry_id": self.registry_id,
                "registry_fingerprint": self.registry_fingerprint,
                "runtime_id": self.runtime_id,
                "role": self.role,
                "resolved_at": self.resolved_at,
                "override": self.override,
            },
            "runtime": {
                "profile": self.profile,
                "provider": self.provider,
                "model": self.model,
                "toolsets": list(self.toolsets),
                "timeout_seconds": self.timeout_seconds,
                "source": self.source,
            },
        }

    def preflight_command(self) -> list[str]:
        prompt = str(self.preflight.get("prompt") or DEFAULT_PREFLIGHT_PROMPT)
        command = [
            self.hermes_binary,
            "--profile",
            self.profile,
            "chat",
            "--provider",
            self.provider,
            "--model",
            self.model,
            "--toolsets",
            "",
            "--source",
            "den-runtime-preflight",
            "-q",
            prompt,
        ]
        return command


def resolve_role_runtime(
    role: str,
    *,
    registry_path: str | Path | None = None,
    run_id: str | None = None,
    overrides: Mapping[str, Any] | None = None,
    allow_runtime_override: bool = False,
    override_reason: str | None = None,
    requested_by: str | None = None,
) -> ResolvedRuntime:
    """Resolve a Den worker role to a spawned-Hermes runtime configuration.

    RETIRED (2026-06-19): The spawned-Hermes YAML runtime registry is
    no longer the live worker-pool runtime authority.  This function
    now fails closed with a clear migration message.

    Live worker-pool runtime authority:
      - Den Core pool members / assignments / leases
      - Channels direct-agent delivery / adapter bindings / wake
      - Concrete Hermes profile / service config
      - Pi Crew runtime back-end

    If you need the old YAML-based resolution (e.g. for tests or legacy
    scripts), call ``_legacy_resolve_role_runtime`` directly with an
    explicit ``registry_path`` argument pointing at the retired YAML.
    """
    raise RuntimeRegistryError(
        "The spawned-Hermes YAML runtime registry is RETIRED (2026-06-19). "
        "Live worker-pool runtime authority comes from Den Core pool "
        "members/assignments/leases and Channels direct-agent delivery, "
        "not the old YAML file. "
        "Call _legacy_resolve_role_runtime() with an explicit registry_path "
        "if you need the old YAML resolution for test fixtures or "
        "historical reference. "
        f"Requested role: {role}, registry_path: {registry_path or DEFAULT_RUNTIME_REGISTRY_PATH}"
    )


def _legacy_resolve_role_runtime(
    role: str,
    *,
    registry_path: str | Path | None = None,
    run_id: str | None = None,
    overrides: Mapping[str, Any] | None = None,
    allow_runtime_override: bool = False,
    override_reason: str | None = None,
    requested_by: str | None = None,
) -> ResolvedRuntime:
    """Resolve a Den worker role to a safe spawned-Hermes runtime configuration."""

    path = _registry_path(registry_path)
    raw_text = path.read_text()
    try:
        registry = yaml.safe_load(raw_text)
    except Exception as exc:  # noqa: BLE001 - preserve concise fail-closed diagnostics
        raise RuntimeRegistryError(f"Malformed runtime registry YAML at {path}: {_redact_secretish(exc)}") from exc
    if not isinstance(registry, Mapping):
        raise RuntimeRegistryError(f"Runtime registry at {path} must be a mapping")

    schema_version = registry.get("schema_version")
    if schema_version != 1:
        raise RuntimeRegistryError(f"Unsupported runtime registry schema_version: {schema_version!r}")

    defaults = _mapping(registry.get("defaults"), "defaults")
    roles = _mapping(registry.get("roles"), "roles")
    missing_roles = sorted(CANONICAL_ROLES - set(roles))
    if missing_roles:
        raise RuntimeRegistryError(f"Runtime registry missing required role entries: {', '.join(missing_roles)}")

    aliases = registry.get("role_aliases") or {}
    if not isinstance(aliases, Mapping):
        raise RuntimeRegistryError("role_aliases must be a mapping when present")
    canonical_role = str(aliases.get(role, role))
    if canonical_role not in CANONICAL_ROLES:
        raise RuntimeRegistryError(f"Unknown spawned-Hermes worker role: {role}")

    role_entry = dict(_mapping(roles.get(canonical_role), f"roles.{canonical_role}"))
    override_block: Mapping[str, Any] | None = None
    if overrides:
        if not allow_runtime_override or not override_reason or not requested_by:
            raise RuntimeRegistryError(
                "Runtime overrides require allow_runtime_override=true, override_reason, and requested_by"
            )
        allowed_override_fields = {"profile", "provider", "model", "toolsets", "timeout_seconds", "runtime_id"}
        unknown_override_fields = sorted(set(overrides) - allowed_override_fields)
        if unknown_override_fields:
            raise RuntimeRegistryError(f"Unsupported runtime override fields: {', '.join(unknown_override_fields)}")
        _reject_secretish_mapping(overrides, "overrides")
        role_entry.update(dict(overrides))
        override_block = {
            "reason": override_reason,
            "requested_by": requested_by,
            "fields": dict(overrides),
        }

    registry_id = _required_str(registry, "registry_id")
    substrate = _required_str(defaults, "substrate")
    if substrate != "spawned_hermes":
        raise RuntimeRegistryError(f"defaults.substrate must be spawned_hermes, got {substrate!r}")

    profile = _required_role_str(role_entry, defaults, "profile", canonical_role)
    provider = _required_role_str(role_entry, defaults, "provider", canonical_role)
    model = _required_role_str(role_entry, defaults, "model", canonical_role)
    runtime_id = _required_str(role_entry, f"roles.{canonical_role}.runtime_id")
    toolsets = _resolve_toolsets(role_entry.get("toolsets", defaults.get("toolsets")), canonical_role)
    timeout_seconds = _resolve_timeout(role_entry.get("timeout_seconds", defaults.get("timeout_seconds")), canonical_role)
    workdir = _required_abs_path(role_entry.get("workdir", defaults.get("workdir")), "workdir")
    run_root = _required_abs_path(role_entry.get("run_root", defaults.get("run_root")), "run_root")
    artifact_filename = _required_str(defaults, "defaults.artifact_filename")
    log_filename = _required_str(defaults, "defaults.log_filename")
    hermes_binary = _required_str(defaults, "defaults.hermes_binary")
    launch = role_entry.get("launch") or {}
    if not isinstance(launch, Mapping):
        raise RuntimeRegistryError(f"roles.{canonical_role}.launch must be a mapping")
    source = str(launch.get("source") or "den-worker")
    extra_args = _resolve_extra_args(launch.get("extra_args") or (), canonical_role)
    preflight = _merge_preflight(defaults.get("preflight"), role_entry.get("preflight"))

    artifact_path = None
    log_path = None
    if run_id is not None:
        artifact_path = str(Path(run_root) / run_id / artifact_filename)
        log_path = str(Path(run_root) / run_id / log_filename)

    fingerprint = "sha256:" + hashlib.sha256(raw_text.encode()).hexdigest()
    resolved_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return ResolvedRuntime(
        schema_version=schema_version,
        registry_id=registry_id,
        registry_path=str(path),
        registry_fingerprint=fingerprint,
        resolved_at=resolved_at,
        role=canonical_role,
        runtime_id=runtime_id,
        substrate=substrate,
        hermes_binary=hermes_binary,
        profile=profile,
        provider=provider,
        model=model,
        toolsets=toolsets,
        timeout_seconds=timeout_seconds,
        workdir=workdir,
        run_root=run_root,
        artifact_filename=artifact_filename,
        log_filename=log_filename,
        source=source,
        extra_args=extra_args,
        preflight=preflight,
        artifact_path=artifact_path,
        log_path=log_path,
        override=override_block,
    )


def _registry_path(registry_path: str | Path | None) -> Path:
    if registry_path is not None:
        return Path(registry_path)
    env_path = os.environ.get("DEN_HERMES_RUNTIME_REGISTRY")
    if env_path:
        return Path(env_path)
    return DEFAULT_RUNTIME_REGISTRY_PATH


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimeRegistryError(f"Runtime registry field {field} must be a mapping")
    _reject_secretish_mapping(value, field)
    return value


def _required_str(mapping: Mapping[str, Any], field: str) -> str:
    key = field.rsplit(".", 1)[-1]
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeRegistryError(f"Runtime registry missing required string field: {field}")
    if SECRETISH_PATTERN.search(value):
        raise RuntimeRegistryError(f"Runtime registry field {field} contains secret-looking value: [REDACTED]")
    return value


def _required_role_str(role_entry: Mapping[str, Any], defaults: Mapping[str, Any], field: str, role: str) -> str:
    required_flag = bool(defaults.get(f"{field}_required", True))
    value = role_entry.get(field)
    if value is None and not required_flag:
        return ""
    if not isinstance(value, str) or not value.strip():
        raise RuntimeRegistryError(f"Runtime role {role} missing required {field}")
    if SECRETISH_PATTERN.search(value):
        raise RuntimeRegistryError(f"Runtime role {role} {field} contains secret-looking value: [REDACTED]")
    return value


def _resolve_toolsets(value: Any, role: str) -> tuple[str, ...]:
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",")]
    elif isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value]
    else:
        raise RuntimeRegistryError(f"Runtime role {role} toolsets must be a non-empty list or CSV string")
    toolsets = tuple(item for item in items if item)
    if not toolsets:
        raise RuntimeRegistryError(f"Runtime role {role} toolsets must not be empty")
    for toolset in toolsets:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", toolset):
            raise RuntimeRegistryError(f"Runtime role {role} has invalid toolset name: {toolset!r}")
    return toolsets


def _resolve_timeout(value: Any, role: str) -> int:
    if not isinstance(value, int) or value <= 0 or value > 86_400:
        raise RuntimeRegistryError(f"Runtime role {role} timeout_seconds must be a positive integer <= 86400")
    return value


def _required_abs_path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeRegistryError(f"Runtime registry missing required path field: {field}")
    if SECRETISH_PATTERN.search(value):
        raise RuntimeRegistryError(f"Runtime registry path field {field} contains secret-looking value: [REDACTED]")
    path = Path(value)
    if not path.is_absolute():
        raise RuntimeRegistryError(f"Runtime registry path field {field} must be absolute: {value!r}")
    return str(path)


def _resolve_extra_args(value: Any, role: str) -> tuple[str, ...]:
    if isinstance(value, str):
        raise RuntimeRegistryError(f"Runtime role {role} launch.extra_args must be a list, not a string")
    if not isinstance(value, (list, tuple)):
        raise RuntimeRegistryError(f"Runtime role {role} launch.extra_args must be a list")
    args = tuple(str(item) for item in value)
    unsafe = [arg for arg in args if SECRETISH_PATTERN.search(arg)]
    if unsafe:
        raise RuntimeRegistryError(f"Runtime role {role} launch.extra_args contains secret-looking value: [REDACTED]")
    allowed_prefixes = ("--reasoning-effort", "--max-iterations")
    for arg in args:
        if arg.startswith("--") and not arg.startswith(allowed_prefixes):
            raise RuntimeRegistryError(f"Runtime role {role} launch.extra_args contains unsupported flag: {arg}")
    return args


def _merge_preflight(default_preflight: Any, role_preflight: Any) -> Mapping[str, Any]:
    merged: dict[str, Any] = {}
    if isinstance(default_preflight, Mapping):
        merged.update(default_preflight)
    if isinstance(role_preflight, Mapping):
        merged.update(role_preflight)
    merged.setdefault("enabled", True)
    merged.setdefault("prompt", DEFAULT_PREFLIGHT_PROMPT)
    merged.setdefault("expected_substring", DEFAULT_PREFLIGHT_EXPECTED)
    merged.setdefault("timeout_seconds", 300)
    _reject_secretish_mapping(merged, "preflight")
    return merged


def _reject_secretish_text(raw_text: str) -> None:
    # Catch explicit key/token fields or obvious provider key values while keeping diagnostics redacted.
    if SECRETISH_PATTERN.search(raw_text):
        raise RuntimeRegistryError("Runtime registry contains secret-looking value: [REDACTED]")


def _reject_secretish_mapping(value: Mapping[str, Any], field: str) -> None:
    for key, item in value.items():
        key_text = str(key)
        if SECRETISH_PATTERN.search(key_text):
            raise RuntimeRegistryError(f"Runtime registry field {field}.{_redact_secretish(key_text)} is not allowed")
        if isinstance(item, Mapping):
            _reject_secretish_mapping(item, f"{field}.{key_text}")
        elif isinstance(item, (list, tuple)):
            for child in item:
                if isinstance(child, Mapping):
                    _reject_secretish_mapping(child, f"{field}.{key_text}")
                elif SECRETISH_PATTERN.search(str(child)):
                    raise RuntimeRegistryError(f"Runtime registry field {field}.{key_text} contains secret-looking value: [REDACTED]")
        elif item is not None and SECRETISH_PATTERN.search(str(item)):
            raise RuntimeRegistryError(f"Runtime registry field {field}.{key_text} contains secret-looking value: [REDACTED]")

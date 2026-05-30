#!/usr/bin/env python3
"""Idempotent operator script to provision live spawned-Hermes pool workers.

Reads and validates the central runtime registry at
``/home/agents/runtime/spawned-hermes-runtimes.yaml`` (or a supplied
``--registry`` path), then produces a pool-member registration matrix
for the four live roles:

    reviewer, validator, drift_checker, packet_auditor

The script:

1. Reads and validates the runtime registry (schema, required roles, secret
   leakage).
2. Resolves each live role to its ResolvedRuntime configuration.
3. Verifies that each resolved profile is a ``spawned-*`` identity, NOT
   ``den-hermes-runner`` or any other operator-first profile.
4. Produces a structured pool-member registration matrix with:
   - worker_identity / pool_member_id
   - profile_identity
   - worker_role
   - agent_instance_id template
   - capability tags
   - status
5. Runs a redacted config guard that scans the registry for obvious
   secret/token leakage without dumping secret values.
6. Optionally emits JSON payloads for Den MCP/Core upsert (``--apply``
   mode).

Usage:
    python scripts/provision_pool_workers.py                 # dry-run default
    python scripts/provision_pool_workers.py --registry /path/to/runtimes.yaml
    python scripts/provision_pool_workers.py --apply          # emit JSON payloads

Exit codes:
    0 – all roles verified and ready for provisioning.
    1 – one or more roles failed validation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Sequence

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]  # handled in main()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LIVE_ROLES = ("reviewer", "validator", "drift_checker", "packet_auditor")

# Spawned profile prefix — all live roles must use this prefix.
SPAWNED_PROFILE_PREFIX = "spawned-"

# Operator-only profiles that must NOT be used as role profiles.
FORBIDDEN_PROFILES = frozenset({"den-hermes-runner", "runner", "default"})

# Pattern for detecting obvious secret/token values.
# Uses word boundaries to avoid false positives (e.g. "task-" matching "sk-...").
SECRET_PATTERN = re.compile(
    r"(?i)"
    r"(?:"
    r"\b(sk-[a-z0-9_-]{8,})|"
    r"\bapi[_-]?key\b|"
    r"\bauth[_-]?token\b|"
    r"\bbearer\s+\S+|"
    r"\bsecret(?:s)?\s*[:=]\s*\S+|"
    r"\btoken\s*[:=]\s*\S+"
    r")"
)

# Default registry path.
DEFAULT_REGISTRY = Path("/home/agents/runtime/spawned-hermes-runtimes.yaml")

# Pool member ID prefix for each role.
POOL_MEMBER_PREFIXES: dict[str, str] = {
    "reviewer": "pool-reviewer",
    "validator": "pool-validator",
    "drift_checker": "pool-drift-checker",
    "packet_auditor": "pool-packet-auditor",
}

# Capability tags from the role catalog.
ROLE_CAPABILITIES: dict[str, list[str]] = {
    "coder": ["implementation", "code_generation"],
    "reviewer": ["review", "code_audit"],
    "validator": ["validation", "test_verification"],
    "drift_checker": ["drift_detection", "consistency_check"],
    "packet_auditor": ["audit", "packet_verification"],
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ResolvedPoolMember:
    """A single pool worker registration entry."""

    worker_role: str
    profile_identity: str
    worker_identity: str  # pool member ID
    agent_instance_id_template: str
    pool_member_id: str
    runtime_id: str
    provider: str
    model: str
    capabilities: list[str]
    timeout_seconds: int
    status: str = "pending"  # pending | ready | blocked


@dataclass
class ProvisioningResult:
    """Collective result of the provisioning dry-run or apply."""

    registry_path: str
    registry_id: str
    registry_fingerprint: str
    roles_resolved: int = 0
    roles_failed: int = 0
    members: list[ResolvedPoolMember] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    secrets_found: list[str] = field(default_factory=list)
    credential_guard_ok: bool = True


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def resolve_role_runtime(
    role: str, registry: dict[str, Any], defaults: dict[str, Any], roles: dict[str, Any]
) -> dict[str, Any]:
    """Resolve a single role to its effective runtime config.

    This is a simplified reimplementation that mirrors
    ``den_hermes.runtime_registry.resolve_role_runtime`` without needing
    the full module.  Returns a flat dict with the fields we need.
    """
    aliases = registry.get("role_aliases") or {}
    canonical = str(aliases.get(role, role))

    role_entry = roles.get(canonical)
    if not role_entry:
        raise RuntimeError(f"Role {role!r} (canonical: {canonical!r}) not found in registry")

    def _get(field: str) -> Any:
        return role_entry.get(field, defaults.get(field))

    # These are required at role level when profile_required etc are true.
    profile_required = bool(defaults.get("profile_required", True))
    provider_required = bool(defaults.get("provider_required", True))
    model_required = bool(defaults.get("model_required", True))

    profile = role_entry.get("profile")
    if profile_required and not profile:
        raise RuntimeError(f"Role {role!r} missing required 'profile'")
    provider = role_entry.get("provider")
    if provider_required and not provider:
        raise RuntimeError(f"Role {role!r} missing required 'provider'")
    model = role_entry.get("model")
    if model_required and not model:
        raise RuntimeError(f"Role {role!r} missing required 'model'")

    launch = role_entry.get("launch") or {}
    return {
        "role": canonical,
        "runtime_id": role_entry.get("runtime_id", f"{canonical}-primary"),
        "profile": profile,
        "provider": provider,
        "model": model,
        "toolsets": role_entry.get("toolsets", defaults.get("toolsets", [])),
        "timeout_seconds": role_entry.get("timeout_seconds", defaults.get("timeout_seconds", 900)),
        "source": launch.get("source", "den-worker"),
    }


def check_profile_not_forbidden(profile: str, role: str) -> str | None:
    """Return an error string if the profile is forbidden, else None."""
    if profile in FORBIDDEN_PROFILES:
        return (
            f"Role {role!r} uses forbidden profile {profile!r}. "
            f"Live roles must use {SPAWNED_PROFILE_PREFIX!r}* profiles, "
            f"not operator profiles: {sorted(FORBIDDEN_PROFILES)}"
        )
    if not profile.startswith(SPAWNED_PROFILE_PREFIX):
        return (
            f"Role {role!r} uses profile {profile!r} which does not start "
            f"with expected prefix {SPAWNED_PROFILE_PREFIX!r}"
        )
    return None


def scan_for_secrets(data: Any, path: str = "", results: list[str] | None = None) -> list[str]:
    """Recursively scan config data for obvious secret/token values.

    Returns list of redacted diagnostic strings — never dumps actual values.
    """
    if results is None:
        results = []

    if isinstance(data, dict):
        for key, value in data.items():
            child_path = f"{path}.{key}" if path else key
            # Check key itself for secret-ish name
            if SECRET_PATTERN.search(str(key)):
                results.append(
                    f"{child_path}: key matches secret pattern [REDACTED]"
                )
            scan_for_secrets(value, child_path, results)
    elif isinstance(data, (list, tuple)):
        for idx, item in enumerate(data):
            child_path = f"{path}[{idx}]"
            scan_for_secrets(item, child_path, results)
    elif isinstance(data, str):
        if SECRET_PATTERN.search(data):
            results.append(
                f"{path}: value matches secret pattern [REDACTED]"
            )
    return results


def build_pool_member(
    role: str,
    runtime: dict[str, Any],
) -> ResolvedPoolMember:
    """Build a single ResolvedPoolMember from resolved runtime config."""
    prefix = POOL_MEMBER_PREFIXES.get(role, f"pool-{role}")
    pool_member_id = f"{prefix}-01"
    capabilities = ROLE_CAPABILITIES.get(role, [role])
    agent_instance_template = (
        f"hermes:den-k8:{runtime['profile']}:{pool_member_id}:{{id_suffix}}"
    )

    return ResolvedPoolMember(
        worker_role=role,
        profile_identity=runtime["profile"],
        worker_identity=pool_member_id,
        pool_member_id=pool_member_id,
        agent_instance_id_template=agent_instance_template,
        runtime_id=runtime["runtime_id"],
        provider=runtime["provider"],
        model=runtime["model"],
        capabilities=list(capabilities),
        timeout_seconds=runtime["timeout_seconds"],
        status="ready",
    )


def load_registry(registry_path: Path) -> dict[str, Any]:
    """Load and validate the runtime registry YAML.

    Raises RuntimeError on structural issues or missing fields.
    """
    if not registry_path.exists():
        raise RuntimeError(f"Runtime registry not found: {registry_path}")

    raw_text = registry_path.read_text()
    if yaml is None:
        raise RuntimeError("PyYAML is required. Install with: pip install pyyaml")

    try:
        registry = yaml.safe_load(raw_text)
    except Exception as exc:
        raise RuntimeError(f"Malformed YAML at {registry_path}: {exc}") from exc

    if not isinstance(registry, dict):
        raise RuntimeError(f"Runtime registry at {registry_path} must be a mapping (dict)")

    if registry.get("schema_version") != 1:
        raise RuntimeError(
            f"Unsupported schema_version: {registry.get('schema_version')!r}"
        )

    defaults = registry.get("defaults")
    if not isinstance(defaults, dict):
        raise RuntimeError("Missing or non-dict 'defaults' in registry")

    roles = registry.get("roles")
    if not isinstance(roles, dict):
        raise RuntimeError("Missing or non-dict 'roles' in registry")

    registry_id = registry.get("registry_id")
    if not registry_id or not isinstance(registry_id, str):
        raise RuntimeError("Missing or non-string 'registry_id'")

    if defaults.get("substrate") != "spawned_hermes":
        raise RuntimeError(
            f"defaults.substrate must be 'spawned_hermes', got "
            f"{defaults.get('substrate')!r}"
        )

    return registry


def compute_fingerprint(registry_path: Path) -> str:
    """Compute a SHA-256 hex fingerprint of the raw registry file."""
    import hashlib

    raw = registry_path.read_bytes()
    return hashlib.sha256(raw).hexdigest()[:16]


def run_provision(
    registry_path: Path,
    *,
    apply_mode: bool = False,
    roles: Sequence[str] | None = None,
) -> ProvisioningResult:
    """Run the provisioning dry-run (or apply) and return structured result."""
    result = ProvisioningResult(
        registry_path=str(registry_path),
        registry_id="",
        registry_fingerprint="",
    )

    # 1. Load and validate registry
    try:
        registry = load_registry(registry_path)
    except RuntimeError as exc:
        result.errors.append(str(exc))
        result.roles_failed = len(roles or LIVE_ROLES)
        return result

    registry_id = registry.get("registry_id", "unknown")
    result.registry_id = registry_id
    result.registry_fingerprint = compute_fingerprint(registry_path)

    defaults = registry.get("defaults", {})
    roles_block = registry.get("roles", {})
    target_roles = list(roles) if roles else list(LIVE_ROLES)

    # 2. Scan for secrets in registry config
    secrets = scan_for_secrets(registry)
    result.secrets_found = secrets
    result.credential_guard_ok = len(secrets) == 0
    if secrets:
        result.errors.append(
            f"Credential guard detected {len(secrets)} potential secret "
            f"pattern(s) in registry. Run with --verbose-secrets to list them."
        )

    # 3. Resolve each target role
    for role in target_roles:
        try:
            runtime = resolve_role_runtime(role, registry, defaults, roles_block)
            profile = runtime["profile"]

            # 3a. Check profile is not forbidden
            profile_error = check_profile_not_forbidden(profile, role)
            if profile_error:
                result.errors.append(profile_error)
                result.roles_failed += 1
                member = build_pool_member(role, runtime)
                member.status = "blocked"
                result.members.append(member)
                continue

            # 3b. Build pool member
            member = build_pool_member(role, runtime)
            result.members.append(member)
            result.roles_resolved += 1

        except RuntimeError as exc:
            result.errors.append(f"Role {role!r}: {exc}")
            result.roles_failed += 1
        except Exception as exc:
            result.errors.append(f"Role {role!r}: unexpected error: {exc}")
            result.roles_failed += 1

    # 4. Apply mode: emit JSON payloads
    if apply_mode:
        _emit_apply_payloads(result)

    return result


def _emit_apply_payloads(result: ProvisioningResult) -> None:
    """Print structured JSON payloads for Den MCP/Core upsert."""
    for member in result.members:
        payload = {
            "action": "upsert_pool_member",
            "payload": {
                "pool_member_id": member.pool_member_id,
                "worker_role": member.worker_role,
                "profile_identity": member.profile_identity,
                "worker_identity": member.worker_identity,
                "runtime_id": member.runtime_id,
                "provider": member.provider,
                "model": member.model,
                "capabilities": member.capabilities,
                "timeout_seconds": member.timeout_seconds,
                "status": member.status,
                "agent_instance_id_template": member.agent_instance_id_template,
            },
        }
        print(f"### DEN_MCP_UPSERT {json.dumps(payload, indent=2)}")


def format_matrix(result: ProvisioningResult) -> str:
    """Format the pool-member registration matrix as a human-readable table."""
    lines = [
        f"Pool Worker Provisioning Matrix",
        f"Registry:    {result.registry_id}",
        f"Path:        {result.registry_path}",
        f"Fingerprint: {result.registry_fingerprint}",
        f"Credential guard: {'PASS' if result.credential_guard_ok else 'FAIL'}",
        "",
        f"{'ROLE':<18} {'POOL MEMBER':<22} {'PROFILE':<22} {'PROVIDER':<16} {'MODEL':<20} {'STATUS':<10} CAPABILITIES",
        f"{'----':<18} {'-----------':<22} {'-------':<22} {'--------':<16} {'-----':<20} {'------':<10} ------------",
    ]
    for member in result.members:
        caps = ", ".join(member.capabilities)
        lines.append(
            f"{member.worker_role:<18} {member.pool_member_id:<22} "
            f"{member.profile_identity:<22} {member.provider:<16} "
            f"{member.model:<20} {member.status:<10} {caps}"
        )

    if result.errors:
        lines.append("")
        lines.append("ERRORS:")
        for err in result.errors:
            lines.append(f"  ! {err}")

    if result.secrets_found and result.credential_guard_ok is False:
        lines.append("")
        lines.append(
            f"SECRET GUARD: {len(result.secrets_found)} potential secret "
            f"pattern(s) detected (details redacted above)."
        )

    lines.append("")
    lines.append(
        f"Resolved: {result.roles_resolved} roles "
        f"Failed: {result.roles_failed} roles"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="provision_pool_workers",
        description="Provision live spawned-Hermes pool workers from the central runtime registry",
    )
    parser.add_argument(
        "--registry",
        default=None,
        type=str,
        help=f"Path to runtime registry YAML (default: {DEFAULT_REGISTRY})",
    )
    parser.add_argument(
        "--roles",
        default=",".join(LIVE_ROLES),
        help=f"Comma-separated roles to provision (default: {','.join(LIVE_ROLES)})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Emit JSON upsert payloads for Den MCP/Core (default: dry-run only)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output machine-readable JSON instead of human-readable table",
    )
    parser.add_argument(
        "--verbose-secrets",
        action="store_true",
        help="List redacted secret-pattern diagnostics in output",
    )
    parser.add_argument(
        "--registry-path",
        dest="registry",
        help="Alias for --registry",
    )

    args = parser.parse_args(argv)
    roles = [r.strip() for r in args.roles.split(",") if r.strip()]

    # Resolve registry path
    registry_path = Path(args.registry) if args.registry else DEFAULT_REGISTRY

    # Run provisioning
    result = run_provision(
        registry_path,
        apply_mode=args.apply,
        roles=roles,
    )

    # Output
    if args.json:
        output = {
            "registry_path": result.registry_path,
            "registry_id": result.registry_id,
            "registry_fingerprint": result.registry_fingerprint,
            "credential_guard_ok": result.credential_guard_ok,
            "roles_resolved": result.roles_resolved,
            "roles_failed": result.roles_failed,
            "members": [asdict(m) for m in result.members],
            "errors": result.errors,
        }
        if args.verbose_secrets and result.secrets_found:
            output["secrets_found"] = result.secrets_found
        print(json.dumps(output, indent=2))
    else:
        print(format_matrix(result))

    return 1 if result.roles_failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())

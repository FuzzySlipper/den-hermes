"""Pool runtime drift detection for persistent pool-worker assignments.

This module separates two classes of pool assignment diagnostics:

1. **Blocking drift** — concrete pool-runtime authority evidence problems:
   - Missing pool member identity (``DEN_HERMES_POOL_MEMBER_ID`` unset)
   - Missing or unset pool profile identity (``DEN_HERMES_PROFILE`` unset)
   - Role/profile name mismatch (e.g. role ``coder`` but profile ``spawned-reviewer``)
   - Explicit failure evidence from the worker/gateway or Den metadata.

2. **Informational registry mismatch** — the registry YAML's expected provider/model
   differs from what the pool worker was deployed with.  Because the pool worker's
   own profile/gateway config is the runtime authority, registry differences are
   readback hints, not proof of failure.  They are logged and included in
   diagnostics but do NOT block the assignment.

Design invariant: do NOT read or write ``/home/agents/profiles/<role>/config.yaml``
from this code path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from den_hermes.runtime_registry import ResolvedRuntime

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Profile-name → role mapping for drift checks
# ---------------------------------------------------------------------------

ROLE_FROM_PROFILE: dict[str, str] = {
    "spawned-coder": "coder",
    "spawned-reviewer": "reviewer",
    "spawned-validator": "validator",
    "spawned-drift-checker": "drift_checker",
    "spawned-packet-auditor": "packet_auditor",
}


def _canonical_role_for_profile(profile: str) -> str | None:
    """Return the expected Den worker role for a spawned profile name.

    E.g. ``spawned-coder`` → ``"coder"``.  Returns ``None`` for unknown
    profile names (handled separately by the orchestrator).
    """
    return ROLE_FROM_PROFILE.get(profile)


# ---------------------------------------------------------------------------
# Drift data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PoolRuntimeDrift:
    """Result of a pool runtime drift check.

    ``drifted`` is ``True`` only when a *blocking* problem is found (missing
    identity, role/profile mismatch, explicit failure evidence).

    ``registry_mismatch`` tracks informational differences between the registry
    YAML and the pool worker's actual config.  These are never blocking on
    their own.
    """

    drifted: bool
    diagnostics: dict[str, Any] = field(default_factory=dict)
    details: str = ""

    # Informational — never blocking
    registry_mismatch: bool = False
    registry_mismatch_details: str = ""

    # Pool authority evidence
    runtime_authority: str = "profile_gateway"
    pool_member_id: str | None = None
    pool_profile: str | None = None

    @staticmethod
    def no_drift() -> PoolRuntimeDrift:
        return PoolRuntimeDrift(drifted=False, details="No drift detected.")

    @staticmethod
    def missing_pool_identity(
        *,
        pool_member_id: str | None,
        pool_profile: str | None,
    ) -> PoolRuntimeDrift:
        details_parts: list[str] = []
        details_parts.append("Pool runtime authority evidence missing or incomplete")

        diagnostics: dict[str, Any] = {"pool_member_id": pool_member_id, "pool_profile": pool_profile}
        if not pool_member_id:
            details_parts.append("DEN_HERMES_POOL_MEMBER_ID is missing or empty")
        if not pool_profile:
            details_parts.append("DEN_HERMES_PROFILE is missing or empty")

        details = "; ".join(details_parts)
        return PoolRuntimeDrift(
            drifted=True,
            diagnostics=diagnostics,
            details=details,
            pool_member_id=pool_member_id,
            pool_profile=pool_profile,
        )

    @staticmethod
    def role_profile_mismatch(
        *,
        role: str,
        pool_profile: str,
        expected_role: str | None,
    ) -> PoolRuntimeDrift:
        expected = expected_role or "(unknown profile→role mapping)"
        details = (
            f"Pool profile {pool_profile!r} maps to role {expected}, "
            f"but assignment role is {role!r}"
        )
        return PoolRuntimeDrift(
            drifted=True,
            diagnostics={
                "role": role,
                "pool_profile": pool_profile,
                "expected_role": expected_role,
            },
            details=details,
            pool_profile=pool_profile,
        )

    @staticmethod
    def registry_mismatch_only(
        *,
        registry_provider: str | None,
        registry_model: str | None,
        registry_profile: str | None,
        pool_profile: str | None,
        pool_member_id: str | None,
    ) -> PoolRuntimeDrift:
        """Create a non-blocking drift result for informational registry differences."""
        details_parts: list[str] = []
        diagnostics: dict[str, Any] = {
            "registry_expected": {"profile": registry_profile},
            "pool_actual": {"profile": pool_profile, "pool_member_id": pool_member_id},
        }

        if registry_profile and registry_profile != pool_profile:
            details_parts.append(
                f"registry profile {registry_profile!r} != pool profile {pool_profile!r}"
            )
        if registry_provider:
            diagnostics["registry_expected"]["provider"] = registry_provider
        if registry_model:
            diagnostics["registry_expected"]["model"] = registry_model

        details = "; ".join(details_parts) if details_parts else "registry metadata differs from pool context (informational)"

        return PoolRuntimeDrift(
            drifted=False,
            diagnostics=diagnostics,
            details=details,
            registry_mismatch=True,
            registry_mismatch_details=details,
            pool_profile=pool_profile,
            pool_member_id=pool_member_id,
        )


# ---------------------------------------------------------------------------
# Public drift checker
# ---------------------------------------------------------------------------


def check_pool_runtime_drift(
    *,
    registry_runtime: ResolvedRuntime,
    role: str,
    pool_member_id: str | None,
    pool_profile: str | None,
) -> PoolRuntimeDrift:
    """Check for pool runtime authority drift.

    Blocking conditions (in priority order):
      1. Missing ``pool_member_id`` → block
      2. Missing ``pool_profile`` → block
      3. Role/profile name mismatch → block

    Non-blocking conditions (logged, diagnostics attached, never block):
      4. Registry expected profile ≠ pool profile
      5. Registry expected provider/model ≠ what pool was deployed with

    Parameters
    ----------
    registry_runtime:
        Resolved runtime from the central registry YAML.  Provides expected
        profile/provider/model/timeouts for the role.
    role:
        The Den worker role (``"coder"``, ``"reviewer"``, etc.).
    pool_member_id:
        The concrete pool worker identity (from ``DEN_HERMES_POOL_MEMBER_ID``).
        ``None`` or empty is a blocking drift.
    pool_profile:
        The pool worker's profile name (from ``DEN_HERMES_PROFILE``).
        ``None`` or empty is a blocking drift.

    Returns
    -------
    ``PoolRuntimeDrift`` with ``drifted=True`` for blocking problems,
    ``drifted=False`` otherwise.  Informational registry mismatches are
    carried as ``registry_mismatch=True`` with details in diagnostics.
    """
    # ---- Blocking checks ----

    # 1. Missing pool member identity
    if not pool_member_id:
        return PoolRuntimeDrift.missing_pool_identity(
            pool_member_id=pool_member_id,
            pool_profile=pool_profile,
        )

    # 2. Missing pool profile identity
    if not pool_profile:
        return PoolRuntimeDrift.missing_pool_identity(
            pool_member_id=pool_member_id,
            pool_profile=pool_profile,
        )

    # 3. Role/profile name mismatch
    expected_role = _canonical_role_for_profile(pool_profile)
    if expected_role is not None and expected_role != role:
        return PoolRuntimeDrift.role_profile_mismatch(
            role=role,
            pool_profile=pool_profile,
            expected_role=expected_role,
        )

    # ---- Informational checks (non-blocking) ----

    registry_profile = registry_runtime.profile
    registry_provider = registry_runtime.provider
    registry_model = registry_runtime.model

    registry_differs = (
        (registry_profile is not None and registry_profile != pool_profile)
        or (registry_provider is not None and registry_provider != "")
        or (registry_model is not None and registry_model != "")
    )

    if registry_differs:
        logger.info(
            "Pool runtime registry mismatch (informational): "
            "role=%s pool_member=%s pool_profile=%s "
            "registry_profile=%s registry_provider=%s registry_model=%s",
            role, pool_member_id, pool_profile,
            registry_profile, registry_provider, registry_model,
        )
        return PoolRuntimeDrift.registry_mismatch_only(
            registry_provider=registry_provider,
            registry_model=registry_model,
            registry_profile=registry_profile,
            pool_profile=pool_profile,
            pool_member_id=pool_member_id,
        )

    return PoolRuntimeDrift.no_drift()

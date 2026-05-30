#!/usr/bin/env python3
"""Validate the spawned-Hermes worker role catalog document.

Deterministic checks (no network I/O, no Den API calls):

1. Required role catalog document exists at docs/worker-role-catalog.md.
2. Required Scout design document exists at docs/spawned-scout-design-1691.md.
3. All six roles (coder, reviewer, validator, drift_checker, packet_auditor, scout)
   are defined as H2-level headings in the catalog.
4. All six roles appear in the role summary matrix table at the bottom.
5. Each role section has required sub-fields (toolset, packet type, etc.)
6. The sample registry references the role catalog.
7. Scout design doc cross-references task #1691 and #1779.
8. The role catalog cross-references #1691.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

REQUIRED_ROLES = frozenset({
    "coder",
    "reviewer",
    "validator",
    "drift_checker",
    "packet_auditor",
    "scout",
})

CATALOG_PATH = REPO_ROOT / "docs" / "worker-role-catalog.md"
SCOUT_DESIGN_PATH = REPO_ROOT / "docs" / "spawned-scout-design-1691.md"
SAMPLE_REGISTRY_PATH = REPO_ROOT / "config" / "spawned-hermes-runtimes.sample.yaml"

# Sub-field markers that must appear in each role's H2 section
REQUIRED_ROLE_SUB_FIELDS = frozenset({
    "Profile identity",
    "Worker role",
    "Allowed toolsets",
    "Side-effect envelope",
    "Checkpoint types",
    "Packet type",
    "Cleanup/release",
    "Memory policy",
})


def main() -> int:
    errors: list[str] = []

    # ------------------------------------------------------------------
    # 1. Catalog doc exists
    # ------------------------------------------------------------------
    if not CATALOG_PATH.exists():
        errors.append(f"MISSING: {CATALOG_PATH} — role catalog document not found")
    else:
        catalog_text = CATALOG_PATH.read_text()
        _check_catalog_roles(catalog_text, errors)
        _check_role_summary_matrix(catalog_text, errors)
        _check_catalog_cross_refs(catalog_text, errors)

    # ------------------------------------------------------------------
    # 2. Scout design doc exists
    # ------------------------------------------------------------------
    if not SCOUT_DESIGN_PATH.exists():
        errors.append(f"MISSING: {SCOUT_DESIGN_PATH} — scout design document not found")
    else:
        scout_text = SCOUT_DESIGN_PATH.read_text()
        _check_scout_doc(scout_text, errors)

    # ------------------------------------------------------------------
    # 3. Sample registry references the role catalog
    # ------------------------------------------------------------------
    if not SAMPLE_REGISTRY_PATH.exists():
        errors.append(f"MISSING: {SAMPLE_REGISTRY_PATH} — sample registry not found")
    else:
        registry_text = SAMPLE_REGISTRY_PATH.read_text()
        if "worker-role-catalog.md" not in registry_text:
            errors.append(f"MISSING: {SAMPLE_REGISTRY_PATH} does not reference docs/worker-role-catalog.md")
        if "spawned-scout-design-1691.md" not in registry_text:
            errors.append(f"MISSING: {SAMPLE_REGISTRY_PATH} does not reference docs/spawned-scout-design-1691.md")

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------
    if errors:
        print("ROLE CATALOG VALIDATION FAILED", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print("All validations passed.")
    print(f"  Catalog: {CATALOG_PATH}")
    print(f"  Scout design: {SCOUT_DESIGN_PATH}")
    print(f"  Sample registry: {SAMPLE_REGISTRY_PATH}")
    return 0


# Map of common heading word patterns to canonical role names
HEADING_TO_ROLE = {
    "coder": "coder",
    "reviewer": "reviewer",
    "validator": "validator",
    "checker": "drift_checker",  # "Drift Checker" -> last word "Checker"
    "auditor": "packet_auditor",  # "Packet Auditor" -> last word "Auditor"
    "scout": "scout",
}


def _check_catalog_roles(text: str, errors: list[str]) -> None:
    """Check all six roles are defined as H2 or H3 headings in the catalog."""
    # Find H2 and H3 headings
    found_roles = set()
    for line in text.splitlines():
        line_stripped = line.strip()
        if line_stripped.startswith("### ") or (line_stripped.startswith("## ") and not line_stripped.startswith("### ")):
            # Extract all words and check for role matches
            parts = line_stripped.split()
            for part in parts:
                word = part.lower().rstrip(".,:;!?")
                if word in HEADING_TO_ROLE:
                    found_roles.add(HEADING_TO_ROLE[word])

    for role in sorted(REQUIRED_ROLES):
        if role not in found_roles:
            errors.append(f"MISSING: Role '{role}' has no H2/H3 section heading in {CATALOG_PATH.name}")


def _check_role_summary_matrix(text: str, errors: list[str]) -> None:
    """Check the role summary matrix table includes all six roles."""
    for role in sorted(REQUIRED_ROLES):
        # Check for role name in a table row (starts with |)
        found = False
        for line in text.splitlines():
            if line.startswith("|") and role in line.lower() and "spawned-" in line:
                found = True
                break
        if not found:
            errors.append(f"MISSING: Role '{role}' not found in summary matrix table in {CATALOG_PATH.name}")


def _check_catalog_cross_refs(text: str, errors: list[str]) -> None:
    """Check the catalog cross-references task #1691."""
    if "#1691" not in text and "1691" not in text:
        errors.append(f"MISSING: {CATALOG_PATH.name} does not cross-reference task #1691")


def _check_scout_doc(text: str, errors: list[str]) -> None:
    """Check Scout design document requirements."""
    # Cross-references
    if "#1691" not in text:
        errors.append(f"MISSING: {SCOUT_DESIGN_PATH.name} does not reference task #1691")
    if "#1779" not in text and "worker-role-catalog" not in text:
        errors.append(f"MISSING: {SCOUT_DESIGN_PATH.name} does not reference task #1779 or role catalog")
    if "scout_report" not in text:
        errors.append(f"MISSING: {SCOUT_DESIGN_PATH.name} does not define scout_report schema")
    if "read_only_verified" not in text:
        errors.append(f"MISSING: {SCOUT_DESIGN_PATH.name} does not define read_only_verified marker")
    if "read-only" not in text.lower():
        errors.append(f"MISSING: {SCOUT_DESIGN_PATH.name} does not mention read-only enforcement")
    if "checkpoint" not in text.lower():
        errors.append(f"MISSING: {SCOUT_DESIGN_PATH.name} does not discuss checkpoint integration")
    if "Runner" not in text:
        errors.append(f"MISSING: {SCOUT_DESIGN_PATH.name} does not mention Runner usage")
    if "CANONICAL_ROLES" not in text:
        errors.append(f"MISSING: {SCOUT_DESIGN_PATH.name} does not mention CANONICAL_ROLES")
    if "14. Follow-up" not in text:
        errors.append(f"MISSING: {SCOUT_DESIGN_PATH.name} does not mention follow-up task section (expected §14 or '14. Follow-up')")


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Audit worker profiles for zero medium-term and zero long-term memory.

Run from repo root:
    python scripts/audit_worker_profile_memory.py

Or against a custom profile root:
    python scripts/audit_worker_profile_memory.py --profile-root /home/agents/profiles

Exit codes:
    0  All audited worker profiles pass (no memory found).
    1  One or more worker profiles have memory artifacts or enabled memory.
    2  Audit could not complete (bad paths, unreadable files, etc.).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

# Worker-style profile prefixes/names we look for when scanning a profile root.
WORKER_PREFIXES = ("spawned-coder", "spawned-reviewer", "spawned-validator",
                   "spawned-drift-checker", "spawned-packet-auditor",
                   "spawned-drift_checker", "spawned-packet_auditor")

# Registry roles that are worker-style.
WORKER_ROLES = {"coder", "reviewer", "validator", "drift_checker",
                "packet_auditor", "drift", "audit"}

# Files/directories that indicate long-term or medium-term memory.
LONG_TERM_FILES = ("memories/MEMORY.md", "memories/USER.md")
MEDIUM_TERM_FILES = ("state.db",)
MEDIUM_TERM_DIRS = ("sessions", "checkpoints")

# Config keys that enable memory.  Values that signal "enabled".
MEMORY_CONFIG_PATHS = (
    ("memory", "memory_enabled"),
    ("memory", "user_profile_enabled"),
)

# Config paths that must be absent/blank for worker profiles even when the
# built-in memory booleans are disabled.
MEMORY_PROVIDER_PATHS = (
    ("memory", "provider"),
    ("den_memory",),
)

# Config list paths that register memory tools.
MEMORY_TOOLSET_PATHS = (
    ("platform_toolsets", "cli"),  # list of strings under platform_toolsets.cli
)


def _has_value(obj: Any, path: tuple[str, ...], predicate) -> bool:
    """Walk into *obj* along *path* and apply *predicate* to the leaf."""
    try:
        for key in path:
            if isinstance(obj, dict):
                obj = obj[key]
            elif isinstance(obj, list) and isinstance(key, int):
                obj = obj[key]
            else:
                return False
        return predicate(obj)
    except Exception:
        return False


def _is_truthy(val: Any) -> bool:
    return bool(val)


def _contains_memory(val: Any) -> bool:
    if isinstance(val, list):
        return any(str(v).lower() == "memory" for v in val)
    return False


def _is_nonempty(val: Any) -> bool:
    if val is None:
        return False
    if isinstance(val, str):
        return bool(val.strip())
    if isinstance(val, (dict, list, tuple, set)):
        return bool(val)
    return bool(val)


def _is_yaml_available() -> bool:
    return yaml is not None


def _load_yaml_safe(path: Path) -> Any:
    if yaml is None:
        raise RuntimeError("PyYAML is not installed")
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _redact_config_summary(cfg: dict) -> dict:
    """Return a minimal, secret-safe summary of a config dict."""
    summary: dict[str, Any] = {}
    for section, key in MEMORY_CONFIG_PATHS:
        val = cfg.get(section, {}).get(key) if isinstance(cfg.get(section), dict) else None
        if val is not None:
            summary.setdefault(section, {})[key] = val
    for top, sub in MEMORY_TOOLSET_PATHS:
        lst = cfg.get(top, {}).get(sub) if isinstance(cfg.get(top), dict) else None
        if isinstance(lst, list):
            summary.setdefault(top, {})[sub] = ["memory" in str(v).lower() for v in lst]
    provider = cfg.get("memory", {}).get("provider") if isinstance(cfg.get("memory"), dict) else None
    if provider not in (None, ""):
        summary.setdefault("memory", {})["provider"] = "<nonempty>"
    if _is_nonempty(cfg.get("den_memory")):
        summary["den_memory"] = "<configured>"
    return summary


def audit_profile(profile_path: Path) -> dict[str, Any]:
    """Audit a single profile directory and return a result dict."""
    result: dict[str, Any] = {
        "profile_path": str(profile_path),
        "profile_name": profile_path.name,
        "long_term_findings": [],
        "medium_term_findings": [],
        "config_findings": [],
        "config_summary": {},
        "passed": True,
    }

    # Long-term memory files
    for rel in LONG_TERM_FILES:
        p = profile_path / rel
        if p.exists():
            result["long_term_findings"].append(rel)

    # Medium-term memory artifacts
    for rel in MEDIUM_TERM_FILES:
        p = profile_path / rel
        if p.exists():
            result["medium_term_findings"].append(rel)
    for rel in MEDIUM_TERM_DIRS:
        p = profile_path / rel
        if p.is_dir() and any(p.iterdir()):
            # Count children without listing names in detail to keep output compact
            count = sum(1 for _ in p.iterdir())
            result["medium_term_findings"].append(f"{rel}/ ({count} items)")

    # Config checks
    config_path = profile_path / "config.yaml"
    if config_path.is_file():
        try:
            cfg = _load_yaml_safe(config_path)
            if isinstance(cfg, dict):
                for section, key in MEMORY_CONFIG_PATHS:
                    if _has_value(cfg, (section, key), _is_truthy):
                        result["config_findings"].append(f"{section}.{key}=true")
                for top, sub in MEMORY_TOOLSET_PATHS:
                    if _has_value(cfg, (top, sub), _contains_memory):
                        result["config_findings"].append(f"{top}.{sub} contains 'memory'")
                for path in MEMORY_PROVIDER_PATHS:
                    if _has_value(cfg, path, _is_nonempty):
                        result["config_findings"].append(".".join(path) + " configured")
                result["config_summary"] = _redact_config_summary(cfg)
        except Exception as exc:
            result["config_findings"].append(f"config.yaml unreadable: {exc}")

    if (result["long_term_findings"] or result["medium_term_findings"]
            or result["config_findings"]):
        result["passed"] = False

    return result


def discover_worker_profiles(profile_root: Path) -> list[Path]:
    """Return worker profile directories under *profile_root*."""
    profiles: list[Path] = []
    if not profile_root.is_dir():
        return profiles
    for entry in sorted(profile_root.iterdir()):
        if not entry.is_dir():
            continue
        name = entry.name
        if name.startswith(WORKER_PREFIXES):
            profiles.append(entry)
    return profiles


def discover_worker_profiles_from_registry(registry_path: Path) -> list[Path]:
    """Read the runtime registry and resolve profile names to paths."""
    profiles: list[Path] = []
    if not registry_path.is_file():
        return profiles
    try:
        data = _load_yaml_safe(registry_path)
    except Exception:
        return profiles

    roles = data.get("roles", {}) if isinstance(data, dict) else {}
    profile_root = Path("/home/agents/profiles")
    for role_name, role_cfg in roles.items():
        if role_name not in WORKER_ROLES:
            continue
        profile = role_cfg.get("profile") if isinstance(role_cfg, dict) else None
        if profile:
            p = profile_root / profile
            if p.is_dir() and p not in profiles:
                profiles.append(p)
    return profiles


def run_audit(*, profile_root: Path | None = None,
              registry_path: Path | None = None,
              json_mode: bool = False) -> int:
    """Run the audit and print results.  Returns exit code."""
    profiles: list[Path] = []

    if registry_path and registry_path.is_file():
        profiles.extend(discover_worker_profiles_from_registry(registry_path))

    if profile_root and profile_root.is_dir():
        profiles.extend(discover_worker_profiles(profile_root))

    # Deduplicate while preserving order
    seen: set[Path] = set()
    unique_profiles: list[Path] = []
    for p in profiles:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            unique_profiles.append(p)

    if not unique_profiles:
        msg = "No worker profiles discovered."
        if json_mode:
            print(json.dumps({"overall": "incomplete", "message": msg, "results": []}))
        else:
            print(msg)
        return 2

    results: list[dict[str, Any]] = []
    all_passed = True
    for p in unique_profiles:
        r = audit_profile(p)
        results.append(r)
        if not r["passed"]:
            all_passed = False

    if json_mode:
        print(json.dumps({
            "overall": "passed" if all_passed else "failed",
            "profiles_audited": len(results),
            "results": results,
        }, indent=2))
    else:
        print(f"Audited {len(results)} worker profile(s)")
        print("-" * 40)
        for r in results:
            status = "PASS" if r["passed"] else "FAIL"
            print(f"[{status}] {r['profile_name']}")
            if r["long_term_findings"]:
                print(f"  long-term:  {', '.join(r['long_term_findings'])}")
            if r["medium_term_findings"]:
                print(f"  medium-term: {', '.join(r['medium_term_findings'])}")
            if r["config_findings"]:
                print(f"  config:     {', '.join(r['config_findings'])}")
        print("-" * 40)
        print(f"Overall: {'PASSED' if all_passed else 'FAILED'}")

    return 0 if all_passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit worker profiles for memory compliance.")
    parser.add_argument("--profile-root", type=Path,
                        default=Path("/home/agents/profiles"),
                        help="Directory containing Hermes profiles (default: /home/agents/profiles)")
    parser.add_argument("--registry", type=Path,
                        default=Path("/home/agents/runtime/spawned-hermes-runtimes.yaml"),
                        help="Runtime registry YAML path")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    args = parser.parse_args(argv)

    return run_audit(
        profile_root=args.profile_root,
        registry_path=args.registry,
        json_mode=args.json,
    )


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Install and configure the Den memory provider for named Hermes profiles.

The installer keeps Den-owned code out of upstream Hermes by copying the
``den_hermes`` package plus the ``den`` memory plugin to a shared runtime root,
then symlinking ``$HERMES_HOME/plugins/den`` for only the selected profiles.
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

import yaml

DEFAULT_SHARED_ROOT = Path("/home/agents/runtime/den-hermes-memory-provider")
DEFAULT_PROFILES_ROOT = Path("/home/agents/profiles")
PROVIDER_NAME = "den"
BASE_URL = "http://192.168.1.10:18080/den-core-api"
PROJECT_ID = "den-hermes-bridge"

PROFILE_POLICIES: dict[str, dict[str, Any]] = {
    "researcher": {
        "read_spaces": ["assistant:researcher"],
        "write_spaces": ["assistant:researcher"],
        "default_write_space": "assistant:researcher",
    },
    "reviewer": {
        "read_spaces": ["assistant:reviewer", "knowledge_base:den-memory-smoke"],
        "write_spaces": ["assistant:reviewer", "knowledge_base:den-memory-smoke"],
        "default_write_space": "assistant:reviewer",
    },
    "system-architect": {
        "read_spaces": ["assistant:system-architect", "knowledge_base:den-memory-smoke"],
        "write_spaces": ["assistant:system-architect", "knowledge_base:den-memory-smoke"],
        "default_write_space": "assistant:system-architect",
    },
}


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def copy_tree_atomic(source: Path, target: Path) -> None:
    tmp = target.with_name(target.name + ".tmp")
    if tmp.exists() or tmp.is_symlink():
        if tmp.is_dir() and not tmp.is_symlink():
            shutil.rmtree(tmp)
        else:
            tmp.unlink()
    shutil.copytree(source, tmp, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
    if target.exists() or target.is_symlink():
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    tmp.rename(target)


def install_shared(root: Path = DEFAULT_SHARED_ROOT) -> dict[str, str]:
    root.mkdir(parents=True, exist_ok=True)
    copy_tree_atomic(repo_root() / "den_hermes", root / "den_hermes")
    copy_tree_atomic(repo_root() / "plugins" / "den", root / PROVIDER_NAME)
    return {"shared_root": str(root), "package": str(root / "den_hermes"), "plugin": str(root / PROVIDER_NAME)}


def load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    return data if isinstance(data, dict) else {}


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def link_profile_provider(profile_home: Path, shared_root: Path) -> Path:
    plugin_link = profile_home / "plugins" / PROVIDER_NAME
    plugin_link.parent.mkdir(parents=True, exist_ok=True)
    target = shared_root / PROVIDER_NAME
    if plugin_link.exists() or plugin_link.is_symlink():
        if plugin_link.is_symlink() or plugin_link.is_file():
            plugin_link.unlink()
        elif plugin_link.resolve() != target.resolve():
            shutil.rmtree(plugin_link)
    if not plugin_link.exists():
        plugin_link.symlink_to(target, target_is_directory=True)
    return plugin_link


def configure_profile(profile_home: Path, profile: str, *, base_url: str = BASE_URL) -> bool:
    if profile not in PROFILE_POLICIES:
        raise SystemExit(f"refusing to configure unapproved profile {profile!r}")
    config_path = profile_home / "config.yaml"
    data = load_yaml(config_path)
    before = yaml.safe_dump(data, sort_keys=False)
    memory = data.setdefault("memory", {})
    if not isinstance(memory, dict):
        raise SystemExit(f"{config_path}: memory must be a mapping")
    memory["provider"] = PROVIDER_NAME
    policy = PROFILE_POLICIES[profile]
    data["den_memory"] = {
        "enabled": True,
        "deny_auto_behavior": True,
        "project_id": PROJECT_ID,
        "profile": profile,
        "read_spaces": list(policy["read_spaces"]),
        "write_spaces": list(policy["write_spaces"]),
        "default_write_space": policy["default_write_space"],
        "rest": {
            "base_url": base_url,
            "timeout_seconds": 10,
            "retry_attempts": 1,
        },
    }
    after = yaml.safe_dump(data, sort_keys=False)
    if before != after:
        write_yaml(config_path, data)
        return True
    return False


def verify_profile(profile_home: Path, profile: str, shared_root: Path, *, base_url: str = BASE_URL) -> list[str]:
    problems: list[str] = []
    cfg = load_yaml(profile_home / "config.yaml")
    link = profile_home / "plugins" / PROVIDER_NAME
    if not link.exists():
        problems.append(f"missing provider plugin link {link}")
    elif link.resolve() != (shared_root / PROVIDER_NAME).resolve():
        problems.append(f"provider plugin link points to {link.resolve()}, expected {(shared_root / PROVIDER_NAME).resolve()}")
    if (cfg.get("memory") or {}).get("provider") != PROVIDER_NAME:
        problems.append("memory.provider is not den")
    den_cfg = cfg.get("den_memory")
    if not isinstance(den_cfg, dict):
        problems.append("den_memory missing or not mapping")
        return problems
    policy = PROFILE_POLICIES.get(profile)
    if policy is None:
        problems.append(f"unapproved profile {profile!r}")
        return problems
    expected = {
        "enabled": True,
        "deny_auto_behavior": True,
        "project_id": PROJECT_ID,
        "profile": profile,
        "read_spaces": policy["read_spaces"],
        "write_spaces": policy["write_spaces"],
        "default_write_space": policy["default_write_space"],
    }
    for key, expected_value in expected.items():
        if den_cfg.get(key) != expected_value:
            problems.append(f"den_memory.{key}={den_cfg.get(key)!r}, expected {expected_value!r}")
    rest = den_cfg.get("rest") or {}
    if rest.get("base_url") != base_url:
        problems.append(f"den_memory.rest.base_url={rest.get('base_url')!r}, expected {base_url!r}")
    if rest.get("timeout_seconds") != 10:
        problems.append("den_memory.rest.timeout_seconds is not 10")
    if rest.get("retry_attempts") != 1:
        problems.append("den_memory.rest.retry_attempts is not 1")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profiles-root", type=Path, default=DEFAULT_PROFILES_ROOT)
    parser.add_argument("--shared-root", type=Path, default=DEFAULT_SHARED_ROOT)
    parser.add_argument("--profile", action="append", choices=sorted(PROFILE_POLICIES), help="approved profile to configure; repeatable; defaults to all guinea pigs")
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    profiles = args.profile or list(PROFILE_POLICIES)
    installed = {"shared_root": str(args.shared_root), "package": str(args.shared_root / "den_hermes"), "plugin": str(args.shared_root / PROVIDER_NAME)}
    if not args.verify_only:
        installed = install_shared(args.shared_root)

    results: dict[str, Any] = {"installed": installed, "profiles": {}}
    ok = True
    for profile in profiles:
        home = args.profiles_root / profile
        if not home.exists():
            results["profiles"][profile] = {"status": "missing_profile", "home": str(home)}
            ok = False
            continue
        changed = False
        link = home / "plugins" / PROVIDER_NAME
        if not args.verify_only:
            link = link_profile_provider(home, args.shared_root)
            changed = configure_profile(home, profile, base_url=args.base_url)
        problems = verify_profile(home, profile, args.shared_root, base_url=args.base_url)
        results["profiles"][profile] = {
            "status": "ok" if not problems else "failed",
            "home": str(home),
            "plugin": str(link),
            "config_changed": changed,
            "problems": problems,
        }
        ok = ok and not problems

    if args.json:
        print(json.dumps(results, indent=2, sort_keys=True))
    else:
        print(results)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

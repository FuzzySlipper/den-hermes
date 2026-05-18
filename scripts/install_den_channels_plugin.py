#!/usr/bin/env python3
"""Install the Den-owned Hermes Den Channels platform plugin into profiles.

This keeps Den Channels adapter source in the Den-owned den-hermes repo while
making it visible to a clean/upstream Hermes checkout via the normal
$HERMES_HOME/plugins/platforms/<name> user-plugin discovery path.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover - surfaced by main()
    yaml = None  # type: ignore[assignment]

PLUGIN_KEY = "platforms/den_channels"
PLUGIN_MANIFEST_NAME = "den-channels-platform"
DEFAULT_PROFILE_ROOT = Path("/home/agents/profiles")
DEFAULT_SHARED_ROOT = Path("/home/agents/runtime/den-hermes-plugins")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_source() -> Path:
    return repo_root() / "plugins" / "platforms" / "den_channels"


def copy_plugin_source(source: Path, shared_root: Path) -> Path:
    source = source.resolve()
    if not (source / "plugin.yaml").exists() or not (source / "__init__.py").exists():
        raise SystemExit(f"source {source} is not a Hermes plugin directory")
    target = shared_root / "platforms" / "den_channels"
    tmp = target.with_name(target.name + ".tmp")
    if tmp.exists() or tmp.is_symlink():
        if tmp.is_dir() and not tmp.is_symlink():
            shutil.rmtree(tmp)
        else:
            tmp.unlink()
    tmp.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, tmp, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    if target.exists() or target.is_symlink():
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    tmp.replace(target)
    return target


def link_profile_plugin(profile_home: Path, shared_plugin: Path, *, copy: bool = False) -> Path:
    plugin_dir = profile_home / "plugins" / "platforms" / "den_channels"
    plugin_dir.parent.mkdir(parents=True, exist_ok=True)
    if plugin_dir.exists() or plugin_dir.is_symlink():
        if plugin_dir.is_symlink() or plugin_dir.is_file():
            plugin_dir.unlink()
        elif plugin_dir.resolve() != shared_plugin.resolve():
            shutil.rmtree(plugin_dir)
    if copy:
        shutil.copytree(shared_plugin, plugin_dir, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    else:
        plugin_dir.symlink_to(shared_plugin, target_is_directory=True)
    return plugin_dir


def load_config(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise SystemExit("PyYAML is required to update Hermes config.yaml")
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"{path} does not contain a YAML mapping")
    return data


def save_config(path: Path, data: dict[str, Any]) -> None:
    if yaml is None:
        raise SystemExit("PyYAML is required to update Hermes config.yaml")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    path.write_text(text, encoding="utf-8")


def enable_plugin(profile_home: Path) -> bool:
    config_path = profile_home / "config.yaml"
    config = load_config(config_path)
    plugins = config.setdefault("plugins", {})
    if not isinstance(plugins, dict):
        raise SystemExit(f"{config_path}: plugins must be a mapping if present")
    enabled = plugins.setdefault("enabled", [])
    if enabled is None:
        enabled = []
        plugins["enabled"] = enabled
    if not isinstance(enabled, list):
        raise SystemExit(f"{config_path}: plugins.enabled must be a list if present")
    changed = False
    if PLUGIN_KEY not in enabled and PLUGIN_MANIFEST_NAME not in enabled:
        enabled.append(PLUGIN_KEY)
        changed = True
    if changed:
        save_config(config_path, config)
    return changed


def profile_home_for(profile: str, root: Path) -> Path:
    profile_path = Path(profile)
    if profile_path.is_absolute() or "/" in profile or profile.startswith("."):
        return profile_path.expanduser()
    return root / profile


def verify_profile(profile_home: Path) -> list[str]:
    problems: list[str] = []
    plugin_dir = profile_home / "plugins" / "platforms" / "den_channels"
    if not plugin_dir.exists():
        problems.append(f"missing plugin dir {plugin_dir}")
    for name in ("plugin.yaml", "__init__.py", "adapter.py"):
        if not (plugin_dir / name).exists():
            problems.append(f"missing {plugin_dir / name}")
    config_path = profile_home / "config.yaml"
    try:
        config = load_config(config_path)
        enabled = ((config.get("plugins") or {}).get("enabled") or []) if isinstance(config, dict) else []
        if PLUGIN_KEY not in enabled and PLUGIN_MANIFEST_NAME not in enabled:
            problems.append(f"{config_path} does not enable {PLUGIN_KEY}")
    except SystemExit as exc:
        problems.append(str(exc))
    return problems


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=default_source(), help="Den-owned plugin source directory")
    parser.add_argument("--shared-root", type=Path, default=DEFAULT_SHARED_ROOT, help="shared Den-owned plugin install root")
    parser.add_argument("--profile-root", type=Path, default=DEFAULT_PROFILE_ROOT, help="base directory for named Hermes profiles")
    parser.add_argument("--profile", action="append", default=[], help="profile name or absolute HERMES_HOME; repeatable")
    parser.add_argument("--copy-profile", action="store_true", help="copy into each profile instead of symlinking to shared root")
    parser.add_argument("--verify-only", action="store_true", help="only verify profile plugin/config state")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    profiles = args.profile or [p for p in os.environ.get("DEN_HERMES_PLUGIN_PROFILES", "").split(",") if p]
    if not profiles:
        raise SystemExit("provide --profile at least once (e.g. --profile den-channels-runner --profile den-hermes-runner)")

    if args.verify_only:
        shared_plugin = args.shared_root / "platforms" / "den_channels"
    else:
        shared_plugin = copy_plugin_source(args.source, args.shared_root)
        print(f"installed shared plugin: {shared_plugin}")

    any_problem = False
    for profile in profiles:
        home = profile_home_for(profile, args.profile_root)
        if not args.verify_only:
            linked = link_profile_plugin(home, shared_plugin, copy=args.copy_profile)
            changed = enable_plugin(home)
            print(f"profile {profile}: plugin={linked} config_changed={changed}")
        problems = verify_profile(home)
        if problems:
            any_problem = True
            for problem in problems:
                print(f"profile {profile}: ERROR {problem}", file=sys.stderr)
        else:
            print(f"profile {profile}: OK")
    return 1 if any_problem else 0


if __name__ == "__main__":
    raise SystemExit(main())

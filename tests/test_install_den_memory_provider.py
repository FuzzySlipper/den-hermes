from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import yaml

from scripts import install_den_memory_provider as installer

_PLUGIN_PATH = Path(__file__).resolve().parents[1] / "plugins" / "den" / "__init__.py"
_PLUGIN_SPEC = importlib.util.spec_from_file_location("den_memory_plugin_under_test", _PLUGIN_PATH)
assert _PLUGIN_SPEC and _PLUGIN_SPEC.loader
_PLUGIN_MODULE = importlib.util.module_from_spec(_PLUGIN_SPEC)
_PLUGIN_SPEC.loader.exec_module(_PLUGIN_MODULE)
HermesDenMemoryProvider = _PLUGIN_MODULE.HermesDenMemoryProvider


def _profile(root: Path, name: str) -> Path:
    home = root / name
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"memory": {"memory_enabled": True, "user_profile_enabled": True, "provider": ""}}, sort_keys=False),
        encoding="utf-8",
    )
    return home


def test_install_den_memory_provider_configures_only_guinea_pig_profiles(tmp_path: Path) -> None:
    profiles_root = tmp_path / "profiles"
    researcher = _profile(profiles_root, "researcher")
    shared_root = tmp_path / "shared"

    rc = installer.main([
        "--profiles-root", str(profiles_root),
        "--shared-root", str(shared_root),
        "--profile", "researcher",
        "--json",
    ])

    assert rc == 0
    assert (shared_root / "den" / "__init__.py").exists()
    assert (shared_root / "den_hermes" / "memory" / "provider.py").exists()
    assert (researcher / "plugins" / "den").is_symlink()
    assert (researcher / "plugins" / "den").resolve() == (shared_root / "den").resolve()
    cfg = yaml.safe_load((researcher / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["memory"]["provider"] == "den"
    assert cfg["den_memory"] == {
        "enabled": True,
        "deny_auto_behavior": True,
        "project_id": "den-hermes-bridge",
        "profile": "researcher",
        "read_spaces": ["assistant:researcher"],
        "write_spaces": ["assistant:researcher"],
        "default_write_space": "assistant:researcher",
        "rest": {
            "base_url": "http://192.168.1.10:18080/den-core-api",
            "timeout_seconds": 10,
            "retry_attempts": 1,
        },
    }
    assert installer.verify_profile(researcher, "researcher", shared_root) == []


def test_den_memory_plugin_requires_manual_only_config(tmp_path: Path, monkeypatch) -> None:
    hermes_home = tmp_path / "profile"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({
            "memory": {"provider": "den"},
            "den_memory": {
                "enabled": True,
                "deny_auto_behavior": False,
                "project_id": "den-hermes-bridge",
                "profile": "researcher",
                "read_spaces": ["assistant:researcher"],
                "write_spaces": ["assistant:researcher"],
                "default_write_space": "assistant:researcher",
                "rest": {"base_url": "http://192.168.1.10:18080/den-core-api"},
            },
        }, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    provider = HermesDenMemoryProvider()

    assert provider.is_available() is False


def test_den_memory_plugin_exposes_manual_tools_and_dispatches(tmp_path: Path, monkeypatch) -> None:
    hermes_home = tmp_path / "profile"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({
            "memory": {"provider": "den"},
            "den_memory": {
                "enabled": True,
                "deny_auto_behavior": True,
                "project_id": "den-hermes-bridge",
                "profile": "researcher",
                "read_spaces": ["assistant:researcher"],
                "write_spaces": ["assistant:researcher"],
                "default_write_space": "assistant:researcher",
                "rest": {"base_url": "http://192.168.1.10:18080/den-core-api", "timeout_seconds": 10, "retry_attempts": 1},
            },
        }, sort_keys=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    provider = HermesDenMemoryProvider()

    assert provider.is_available() is True
    provider.initialize("session-1", agent_identity="researcher")
    tool_names = {schema["name"] for schema in provider.get_tool_schemas()}
    assert {"den_search", "den_recall", "den_read", "den_list_my_memories", "den_store", "den_update"} <= tool_names
    assert provider.prefetch("anything") == ""
    provider.sync_turn("u", "a") is None

    with patch.object(provider._provider, "den_search", return_value={"status": "ok", "count": 0, "results": []}) as den_search:
        result = json.loads(provider.handle_tool_call("den_search", {"query": "pattern", "limit": 3}))
    assert result["status"] == "ok"
    den_search.assert_called_once_with("pattern", space=None, limit=3)

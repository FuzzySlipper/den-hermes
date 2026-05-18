from __future__ import annotations

from pathlib import Path

import yaml

from scripts import install_den_channels_plugin as installer


def _write_source(root: Path) -> Path:
    source = root / "plugins" / "platforms" / "den_channels"
    source.mkdir(parents=True)
    (source / "plugin.yaml").write_text("name: den-channels-platform\nkind: platform\n", encoding="utf-8")
    (source / "__init__.py").write_text("def register(ctx): pass\n", encoding="utf-8")
    (source / "adapter.py").write_text("class DenChannelsAdapter: pass\n", encoding="utf-8")
    return source


def test_install_den_channels_plugin_symlinks_and_enables_profile(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "repo")
    profile_root = tmp_path / "profiles"
    profile_home = profile_root / "den-channels-runner"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["existing-plugin"]}}, sort_keys=False),
        encoding="utf-8",
    )

    rc = installer.main(
        [
            "--source",
            str(source),
            "--shared-root",
            str(tmp_path / "shared"),
            "--profile-root",
            str(profile_root),
            "--hermes-runtime-root",
            str(tmp_path / "runtime-hermes"),
            "--backup-root",
            str(tmp_path / "backups"),
            "--profile",
            "den-channels-runner",
        ]
    )

    assert rc == 0
    installed = tmp_path / "shared" / "platforms" / "den_channels"
    assert (installed / "adapter.py").read_text(encoding="utf-8") == "class DenChannelsAdapter: pass\n"
    profile_plugin = profile_home / "plugins" / "platforms" / "den_channels"
    assert profile_plugin.is_symlink()
    assert profile_plugin.resolve() == installed.resolve()
    config = yaml.safe_load((profile_home / "config.yaml").read_text(encoding="utf-8"))
    assert config["plugins"]["enabled"] == ["existing-plugin", installer.PLUGIN_KEY]


def test_install_den_channels_plugin_verify_only_detects_missing_enablement(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "repo")
    shared = installer.copy_plugin_source(source, tmp_path / "shared")
    profile_home = tmp_path / "profiles" / "den-hermes-runner"
    installer.link_profile_plugin(profile_home, shared)
    (profile_home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": []}}), encoding="utf-8")

    problems = installer.verify_profile(profile_home)

    assert any(installer.PLUGIN_KEY in problem for problem in problems)


def test_install_den_channels_plugin_is_idempotent(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "repo")
    profile_root = tmp_path / "profiles"
    runtime_root = tmp_path / "runtime-hermes"
    args = [
        "--source",
        str(source),
        "--shared-root",
        str(tmp_path / "shared"),
        "--profile-root",
        str(profile_root),
        "--hermes-runtime-root",
        str(runtime_root),
        "--backup-root",
        str(tmp_path / "backups"),
        "--profile",
        "den-hermes-runner",
    ]

    assert installer.main(args) == 0
    assert installer.main(args) == 0

    config = yaml.safe_load((profile_root / "den-hermes-runner" / "config.yaml").read_text(encoding="utf-8"))
    assert config["plugins"]["enabled"].count(installer.PLUGIN_KEY) == 1


def test_install_den_channels_plugin_replaces_bundled_runtime_copy_with_shared_symlink(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "repo")
    shared = installer.copy_plugin_source(source, tmp_path / "shared")
    runtime_root = tmp_path / "runtime-hermes"
    bundled = runtime_root / "plugins" / "platforms" / "den_channels"
    bundled.mkdir(parents=True)
    (bundled / "adapter.py").write_text("stale bundled source\n", encoding="utf-8")

    link = installer.link_bundled_plugin(runtime_root, shared, tmp_path / "backups")

    assert link.is_symlink()
    assert link.resolve() == shared.resolve()
    backups = list((tmp_path / "backups").glob("*/hermes-agent-plugins-platforms-den_channels/adapter.py"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "stale bundled source\n"


def test_install_den_channels_plugin_can_skip_bundled_runtime_link(tmp_path: Path) -> None:
    source = _write_source(tmp_path / "repo")
    profile_root = tmp_path / "profiles"
    runtime_root = tmp_path / "runtime-hermes"
    bundled = runtime_root / "plugins" / "platforms" / "den_channels"
    bundled.mkdir(parents=True)
    (bundled / "adapter.py").write_text("keep me\n", encoding="utf-8")

    rc = installer.main(
        [
            "--source",
            str(source),
            "--shared-root",
            str(tmp_path / "shared"),
            "--profile-root",
            str(profile_root),
            "--hermes-runtime-root",
            str(runtime_root),
            "--backup-root",
            str(tmp_path / "backups"),
            "--profile",
            "den-hermes-runner",
            "--skip-bundled-link",
        ]
    )

    assert rc == 0
    assert not bundled.is_symlink()
    assert (bundled / "adapter.py").read_text(encoding="utf-8") == "keep me\n"

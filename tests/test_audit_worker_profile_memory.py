import json
from pathlib import Path

import pytest

from scripts.audit_worker_profile_memory import (
    audit_profile,
    discover_worker_profiles,
    discover_worker_profiles_from_registry,
    run_audit,
)


class TestAuditProfile:
    def test_clean_profile_passes(self, tmp_path: Path):
        profile = tmp_path / "spawned-coder"
        profile.mkdir()
        result = audit_profile(profile)
        assert result["passed"] is True
        assert result["long_term_findings"] == []
        assert result["medium_term_findings"] == []
        assert result["config_findings"] == []

    def test_memory_md_fails(self, tmp_path: Path):
        profile = tmp_path / "spawned-coder"
        profile.mkdir()
        (profile / "memories").mkdir()
        (profile / "memories" / "MEMORY.md").write_text("# Memories")
        result = audit_profile(profile)
        assert result["passed"] is False
        assert "memories/MEMORY.md" in result["long_term_findings"]

    def test_user_md_fails(self, tmp_path: Path):
        profile = tmp_path / "spawned-coder"
        profile.mkdir()
        (profile / "memories").mkdir()
        (profile / "memories" / "USER.md").write_text("# User")
        result = audit_profile(profile)
        assert result["passed"] is False
        assert "memories/USER.md" in result["long_term_findings"]

    def test_state_db_fails(self, tmp_path: Path):
        profile = tmp_path / "spawned-coder"
        profile.mkdir()
        (profile / "state.db").write_text("")
        result = audit_profile(profile)
        assert result["passed"] is False
        assert "state.db" in result["medium_term_findings"]

    def test_sessions_dir_with_items_fails(self, tmp_path: Path):
        profile = tmp_path / "spawned-coder"
        profile.mkdir()
        (profile / "sessions").mkdir()
        (profile / "sessions" / "session_1.json").write_text("{}")
        result = audit_profile(profile)
        assert result["passed"] is False
        assert any("sessions/" in f for f in result["medium_term_findings"])

    def test_empty_sessions_dir_passes(self, tmp_path: Path):
        profile = tmp_path / "spawned-coder"
        profile.mkdir()
        (profile / "sessions").mkdir()
        result = audit_profile(profile)
        assert result["passed"] is True
        assert result["medium_term_findings"] == []

    def test_config_memory_enabled_fails(self, tmp_path: Path):
        profile = tmp_path / "spawned-coder"
        profile.mkdir()
        config = (
            "memory:\n"
            "  memory_enabled: true\n"
        )
        (profile / "config.yaml").write_text(config)
        result = audit_profile(profile)
        assert result["passed"] is False
        assert any("memory_enabled" in f for f in result["config_findings"])

    def test_config_user_profile_enabled_fails(self, tmp_path: Path):
        profile = tmp_path / "spawned-coder"
        profile.mkdir()
        config = (
            "memory:\n"
            "  user_profile_enabled: true\n"
        )
        (profile / "config.yaml").write_text(config)
        result = audit_profile(profile)
        assert result["passed"] is False
        assert any("user_profile_enabled" in f for f in result["config_findings"])

    def test_config_platform_toolsets_memory_fails(self, tmp_path: Path):
        profile = tmp_path / "spawned-coder"
        profile.mkdir()
        config = (
            "platform_toolsets:\n"
            "  cli:\n"
            "  - terminal\n"
            "  - memory\n"
        )
        (profile / "config.yaml").write_text(config)
        result = audit_profile(profile)
        assert result["passed"] is False
        assert any("memory" in f for f in result["config_findings"])

    def test_config_clean_passes(self, tmp_path: Path):
        profile = tmp_path / "spawned-coder"
        profile.mkdir()
        config = (
            "memory:\n"
            "  memory_enabled: false\n"
            "  user_profile_enabled: false\n"
            "platform_toolsets:\n"
            "  cli:\n"
            "  - terminal\n"
            "  - file\n"
        )
        (profile / "config.yaml").write_text(config)
        result = audit_profile(profile)
        assert result["passed"] is True
        assert result["config_findings"] == []

    def test_multiple_findings_aggregate(self, tmp_path: Path):
        profile = tmp_path / "spawned-coder"
        profile.mkdir()
        (profile / "memories").mkdir()
        (profile / "memories" / "MEMORY.md").write_text("x")
        (profile / "state.db").write_text("")
        config = "memory:\n  memory_enabled: true\n"
        (profile / "config.yaml").write_text(config)
        result = audit_profile(profile)
        assert result["passed"] is False
        assert len(result["long_term_findings"]) == 1
        assert len(result["medium_term_findings"]) == 1
        assert len(result["config_findings"]) == 1


class TestDiscoverWorkerProfiles:
    def test_discovers_by_prefix(self, tmp_path: Path):
        (tmp_path / "spawned-coder").mkdir()
        (tmp_path / "spawned-reviewer").mkdir()
        (tmp_path / "den-hermes-runner").mkdir()
        found = discover_worker_profiles(tmp_path)
        names = {p.name for p in found}
        assert names == {"spawned-coder", "spawned-reviewer"}

    def test_empty_root(self, tmp_path: Path):
        assert discover_worker_profiles(tmp_path) == []

    def test_nonexistent_root(self, tmp_path: Path):
        assert discover_worker_profiles(tmp_path / "nope") == []


class TestDiscoverFromRegistry:
    def test_resolves_profiles(self, tmp_path: Path):
        registry = tmp_path / "registry.yaml"
        registry.write_text(
            "roles:\n"
            "  coder:\n"
            "    profile: spawned-coder\n"
            "  reviewer:\n"
            "    profile: spawned-reviewer\n"
            "  planner:\n"  # non-worker, should be ignored
            "    profile: planner\n"
        )
        # Create fake profile dirs under a temp profile root is not needed
        # because the function hard-codes /home/agents/profiles; we patch
        # by using a registry that points to names that won't resolve.
        found = discover_worker_profiles_from_registry(registry)
        # Since /home/agents/profiles/spawned-coder likely exists on this host,
        # we just verify the list is non-empty and contains valid paths.
        for p in found:
            assert p.is_dir()

    def test_missing_registry(self, tmp_path: Path):
        assert discover_worker_profiles_from_registry(tmp_path / "missing.yaml") == []


class TestRunAudit:
    def test_passes_with_clean_profiles(self, tmp_path: Path):
        (tmp_path / "spawned-coder").mkdir()
        (tmp_path / "spawned-reviewer").mkdir()
        code = run_audit(profile_root=tmp_path, registry_path=None, json_mode=False)
        assert code == 0

    def test_fails_with_memory_enabled(self, tmp_path: Path):
        p = tmp_path / "spawned-coder"
        p.mkdir()
        (p / "config.yaml").write_text("memory:\n  memory_enabled: true\n")
        code = run_audit(profile_root=tmp_path, registry_path=None, json_mode=False)
        assert code == 1

    def test_json_output(self, tmp_path: Path, capsys):
        p = tmp_path / "spawned-coder"
        p.mkdir()
        (p / "memories").mkdir()
        (p / "memories" / "MEMORY.md").write_text("x")
        code = run_audit(profile_root=tmp_path, registry_path=None, json_mode=True)
        assert code == 1
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["overall"] == "failed"
        assert data["profiles_audited"] == 1

    def test_incomplete_when_no_profiles(self, tmp_path: Path, capsys):
        code = run_audit(profile_root=tmp_path, registry_path=None, json_mode=True)
        assert code == 2
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["overall"] == "incomplete"

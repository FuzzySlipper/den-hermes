from pathlib import Path

from scripts.check_operator_notification_guidance import audit_profile, audit_profiles, main

GOOD_SOUL = """
# Runner

## Operator notifications
- Use mcp_den_send_user_notification / send_user_notification for operator-attention events.
- Blocked work needing operator action sends high urgency notification.
- Notify before tool budget exhaustion or iteration limit when useful tool work remains.
- When assigned queue drains to idle, send exactly one metadata.type=agent_work_complete notification.
"""


def _write_soul(root: Path, profile: str, text: str = GOOD_SOUL) -> Path:
    path = root / profile / "SOUL.md"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    return path


def test_audit_profile_accepts_complete_runner_notification_guidance(tmp_path):
    _write_soul(tmp_path, "den-mcp-runner")

    result = audit_profile(tmp_path, "den-mcp-runner")

    assert result.ok is True
    assert result.missing == []


def test_audit_profile_reports_missing_markers(tmp_path):
    _write_soul(tmp_path, "spawned-orchestrator", "# Orchestrator\nNo notification language here.")

    result = audit_profile(tmp_path, "spawned-orchestrator")

    assert result.ok is False
    assert "send_user_notification" in result.missing
    assert "agent_work_complete" in result.missing
    assert "queue_idle_once" in result.missing


def test_audit_profiles_checks_multiple_loaded_surfaces(tmp_path):
    _write_soul(tmp_path, "den-mcp-runner")
    _write_soul(tmp_path, "den-hermes-runner")
    _write_soul(tmp_path, "spawned-orchestrator")

    results = audit_profiles(tmp_path, ["den-mcp-runner", "den-hermes-runner", "spawned-orchestrator"])

    assert [result.profile for result in results] == ["den-mcp-runner", "den-hermes-runner", "spawned-orchestrator"]
    assert all(result.ok for result in results)


def test_main_returns_nonzero_when_live_surface_missing_guidance(tmp_path, capsys):
    _write_soul(tmp_path, "den-mcp-runner", "# Runner\n")

    code = main(["--profile-root", str(tmp_path), "--profile", "den-mcp-runner"])

    assert code == 1
    assert "MISSING" in capsys.readouterr().out

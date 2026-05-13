import json

import pytest

from den_hermes.den_adapter import DenMcpAdapter


class RecordingMcpTools:
    def __init__(self):
        self.calls = []
        self.completion_response = {"id": 2001}
        self.registration_response = {"summary": "registered worker coder-run (registered)", "worker_run": {"run_id": "coder-run"}}

    def mcp_den_register_worker_run(self, **kwargs):
        self.calls.append(("register_worker_run", kwargs))
        return self.registration_response

    def mcp_den_send_message(self, **kwargs):
        self.calls.append(("send_message", kwargs))
        return {"id": 1001}

    def mcp_den_post_worker_completion_packet(self, **kwargs):
        self.calls.append(("post_worker_completion_packet", kwargs))
        return self.completion_response

    def mcp_den_request_review(self, **kwargs):
        self.calls.append(("request_review", kwargs))
        return {"review_round_id": 321, "message_id": 654}

    def mcp_den_create_review_finding(self, **kwargs):
        finding_id = 9000 + len([call for call in self.calls if call[0] == "create_review_finding"])
        self.calls.append(("create_review_finding", kwargs))
        return {"id": finding_id}

    def mcp_den_post_review_findings(self, **kwargs):
        self.calls.append(("post_review_findings", kwargs))
        return {"message_id": 655}

    def mcp_den_set_review_verdict(self, **kwargs):
        self.calls.append(("set_review_verdict", kwargs))
        return {"ok": True}


def make_adapter(tools):
    return DenMcpAdapter(
        tools=tools,
        project_id="den-hermes-bridge",
        requested_by="den-hermes-runner",
        base_branch="main",
        base_commit="a" * 40,
    )


def test_den_mcp_adapter_registers_spawned_hermes_worker_run_with_expected_payload():
    tools = RecordingMcpTools()
    adapter = make_adapter(tools)

    result = adapter.register_worker_run(
        task_id=1368,
        run_id="coder-run",
        session_id="session-run",
        role="coder",
        branch="task/1368-hermes-native-delegation-exploration",
        base_branch="main",
        base_commit="a" * 40,
        head_commit="b" * 40,
        profile="den-hermes-worker",
        provider="openrouter",
        model="anthropic/claude-sonnet-4",
        toolsets=["terminal", "file"],
        workdir="/home/dev/den-hermes",
        host="den-k8plus",
        timeout_seconds=600,
        artifact_path="/tmp/den-hermes/coder-run/completion.json",
        log_path="/tmp/den-hermes/coder-run/worker.log",
        prompt_packet_message_id=5791,
        dedupe_key="1368:coder:coder-run",
    )

    assert result == tools.registration_response
    assert tools.calls == [
        (
            "register_worker_run",
            {
                "project_id": "den-hermes-bridge",
                "task_id": 1368,
                "requested_by": "den-hermes-runner",
                "role": "coder",
                "substrate": "spawned_hermes",
                "run_id": "coder-run",
                "session_id": "session-run",
                "branch": "task/1368-hermes-native-delegation-exploration",
                "base_branch": "main",
                "base_commit": "a" * 40,
                "head_commit": "b" * 40,
                "profile": "den-hermes-worker",
                "provider": "openrouter",
                "model": "anthropic/claude-sonnet-4",
                "toolsets": "terminal,file",
                "workdir": "/home/dev/den-hermes",
                "host": "den-k8plus",
                "timeout_seconds": 600,
                "artifact_path": "/tmp/den-hermes/coder-run/completion.json",
                "log_path": "/tmp/den-hermes/coder-run/worker.log",
                "prompt_packet_message_id": 5791,
                "dedupe_key": "1368:coder:coder-run",
            },
        )
    ]


def test_den_mcp_adapter_rejects_failed_registration_before_completion_post():
    tools = RecordingMcpTools()
    tools.registration_response = {"error": "task_id 1368 was not found"}
    adapter = make_adapter(tools)

    with pytest.raises(RuntimeError) as excinfo:
        adapter.register_worker_run(task_id=1368, run_id="coder-run", role="coder")

    assert "registration" in str(excinfo.value)
    assert "task_id 1368" in str(excinfo.value)
    assert [name for name, _ in tools.calls] == ["register_worker_run"]


def test_den_mcp_adapter_rejects_mismatched_registration_run_id():
    tools = RecordingMcpTools()
    tools.registration_response = {"worker_run": {"run_id": "different-run"}}
    adapter = make_adapter(tools)

    with pytest.raises(RuntimeError) as excinfo:
        adapter.register_worker_run(task_id=1368, run_id="coder-run", role="coder")

    assert "different-run" in str(excinfo.value)
    assert "coder-run" in str(excinfo.value)


def test_den_mcp_adapter_posts_completion_after_registered_run_with_same_identity():
    tools = RecordingMcpTools()
    adapter = make_adapter(tools)

    adapter.register_worker_run(task_id=1368, run_id="coder-run", session_id="session-run", role="coder")
    adapter.mark_worker_completed(
        task_id=1368,
        run_id="coder-run",
        role="coder",
        artifact={
            "status": "completed",
            "branch": "task/1368-hermes-native-delegation-exploration",
            "head_commit": "b" * 40,
            "tests_run": [{"command": "python -m pytest -q", "result": "12 passed"}],
            "summary": "Implemented successfully.",
        },
    )

    assert [name for name, _ in tools.calls] == ["register_worker_run", "post_worker_completion_packet"]
    assert tools.calls[0][1]["run_id"] == tools.calls[1][1]["run_id"] == "coder-run"


def test_den_mcp_adapter_requests_review_with_verified_coder_metadata():
    tools = RecordingMcpTools()
    adapter = make_adapter(tools)

    result = adapter.request_review(
        task_id=1368,
        branch="task/1368-hermes-native-delegation-exploration",
        head_commit="b" * 40,
        tests_run=[{"command": "python -m pytest -q", "result": "12 passed"}],
        coder_run_id="coder-run",
    )

    assert result == {"review_round_id": 321, "message_id": 654}
    assert tools.calls == [
        (
            "request_review",
            {
                "project_id": "den-hermes-bridge",
                "task_id": 1368,
                "requested_by": "den-hermes-runner",
                "branch": "task/1368-hermes-native-delegation-exploration",
                "base_branch": "main",
                "base_commit": "a" * 40,
                "head_commit": "b" * 40,
                "tests_run": json.dumps([{"command": "python -m pytest -q", "result": "12 passed"}]),
                "notes": "Spawned-Hermes coder run coder-run produced verified branch/head evidence.",
                "run_id": "coder-run",
            },
        )
    ]


def test_den_mcp_adapter_posts_reviewer_findings_and_verdict():
    tools = RecordingMcpTools()
    adapter = make_adapter(tools)

    adapter.post_review_findings(
        task_id=1368,
        review_request={"review_round_id": 321, "message_id": 654},
        reviewer_run_id="reviewer-run",
        verdict="changes_requested",
        findings=[
            {
                "category": "blocking_bug",
                "summary": "Reviewer found a blocker",
                "notes": "Detailed finding notes",
                "file_references": ["den_hermes/worker_launcher.py:10"],
                "test_commands": ["python -m pytest -q"],
            }
        ],
        summary="Reviewer requested changes.",
    )

    assert tools.calls == [
        (
            "create_review_finding",
            {
                "review_round_id": 321,
                "created_by": "den-hermes-runner-reviewer",
                "category": "blocking_bug",
                "summary": "Reviewer found a blocker",
                "notes": "Detailed finding notes",
                "file_references": json.dumps(["den_hermes/worker_launcher.py:10"]),
                "test_commands": json.dumps(["python -m pytest -q"]),
                "run_id": "reviewer-run",
                "subagent_role": "reviewer",
            },
        ),
        (
            "post_review_findings",
            {
                "project_id": "den-hermes-bridge",
                "task_id": 1368,
                "review_round_id": 321,
                "sender": "den-hermes-runner-reviewer",
                "thread_id": 654,
                "notes": "Reviewer requested changes.",
                "run_id": "reviewer-run",
                "subagent_role": "reviewer",
            },
        ),
        (
            "set_review_verdict",
            {
                "review_round_id": 321,
                "verdict": "changes_requested",
                "decided_by": "den-hermes-runner-reviewer",
                "notes": "Reviewer requested changes.",
                "run_id": "reviewer-run",
                "subagent_role": "reviewer",
            },
        ),
    ]


def test_den_mcp_adapter_posts_worker_completion_packets_for_completed_and_failed_runs():
    tools = RecordingMcpTools()
    adapter = make_adapter(tools)

    adapter.mark_worker_completed(
        task_id=1368,
        run_id="coder-run",
        role="coder",
        artifact={
            "status": "completed",
            "branch": "task/1368-hermes-native-delegation-exploration",
            "head_commit": "b" * 40,
            "tests_run": [{"command": "python -m pytest -q", "result": "12 passed"}],
            "summary": "Implemented successfully.",
        },
    )
    adapter.mark_worker_failed(task_id=1368, run_id="reviewer-run", role="reviewer", error="missing artifact")

    assert tools.calls == [
        (
            "post_worker_completion_packet",
            {
                "project_id": "den-hermes-bridge",
                "run_id": "coder-run",
                "requested_by": "den-hermes-runner",
                "status": "completed",
                "role": "coder",
                "packet_type": "implementation_packet",
                "summary": "Implemented successfully.",
                "branch": "task/1368-hermes-native-delegation-exploration",
                "head_commit": "b" * 40,
                "base_commit": "a" * 40,
                "tests_run": json.dumps([{"command": "python -m pytest -q", "result": "12 passed"}]),
                "dedupe_key": "coder-run:completed",
            },
        ),
        (
            "post_worker_completion_packet",
            {
                "project_id": "den-hermes-bridge",
                "run_id": "reviewer-run",
                "requested_by": "den-hermes-runner",
                "status": "failed",
                "role": "reviewer",
                "packet_type": "worker_failure_packet",
                "summary": "missing artifact",
                "failure_category": "spawned_hermes_worker_failed",
                "recovery_guidance": "Inspect spawned-Hermes stdout/stderr and completion artifact path, then rerun or abort the local worker.",
                "dedupe_key": "reviewer-run:failed",
            },
        ),
    ]


def test_den_mcp_adapter_fails_closed_when_worker_completion_is_rejected():
    tools = RecordingMcpTools()
    tools.completion_response = {
        "completion_state": "missing_run",
        "failure_category": "missing_worker_run",
        "summary": "Worker run/session 'smoke-run' was not found in project 'den-hermes-bridge'.",
        "diagnostics": ["Worker run/session 'smoke-run' was not found in project 'den-hermes-bridge'."],
    }
    adapter = make_adapter(tools)

    with pytest.raises(RuntimeError) as excinfo:
        adapter.mark_worker_completed(
            task_id=1370,
            run_id="smoke-run",
            role="coder",
            artifact={
                "status": "completed",
                "branch": "main",
                "head_commit": "b" * 40,
                "tests_run": [{"command": "python -m pytest -q", "result": "15 passed"}],
                "summary": "Synthetic smoke completion.",
            },
        )

    assert "missing_run" in str(excinfo.value)
    assert "smoke-run" in str(excinfo.value)

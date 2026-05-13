import json

from den_hermes.den_adapter import DenMcpAdapter


class RecordingMcpTools:
    def __init__(self):
        self.calls = []

    def mcp_den_send_message(self, **kwargs):
        self.calls.append(("send_message", kwargs))
        return {"id": 1001}

    def mcp_den_post_worker_completion_packet(self, **kwargs):
        self.calls.append(("post_worker_completion_packet", kwargs))
        return {"id": 2001}

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

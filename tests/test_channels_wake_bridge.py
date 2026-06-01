import json
from pathlib import Path

from den_hermes.channels_bridge import (
    DenChannelsResponseBridge,
    DenChannelsWakeBridge,
    InMemoryWakeStore,
    JsonFileWakeStore,
    SpawnedHermesProfileWakeTransport,
    WakeResult,
)


class RecordingDenTools:
    def __init__(self, bindings):
        self.bindings = bindings
        self.agent_stream_messages = []
        self.project_messages = []
        self.user_notifications = []

    def mcp_den_list_agent_instance_bindings(self, **kwargs):
        self.last_binding_query = kwargs
        return {"bindings": self.bindings}

    def mcp_den_send_agent_stream_message(self, **kwargs):
        self.agent_stream_messages.append(kwargs)
        return {"id": 9001}

    def mcp_den_send_message(self, **kwargs):
        self.project_messages.append(kwargs)
        return {"id": 9002}

    def mcp_den_send_user_notification(self, **kwargs):
        self.user_notifications.append(kwargs)
        return {"id": 9003}


class RecordingGatewayClient:
    def __init__(self):
        self.channel_messages = []

    def post_channel_message(self, **kwargs):
        self.channel_messages.append(kwargs)
        return {"id": "channel-reply-1"}


class RecordingHermesTransport:
    def __init__(self):
        self.wakes = []

    def wake_profile(self, *, binding, envelope):
        self.wakes.append({"binding": binding, "envelope": envelope})
        return {"session_id": "hermes-session-1", "external_message_id": "spool-1"}


def delivery(**overrides):
    base = {
        "delivery_request_id": 123,
        "attempt_id": 456,
        "delivery_mode": "wake",
        "dedupe_key": "channel-message:789:wake:den-hermes-runner",
        "correlation_id": "corr-789",
        "target": {"agent_identity": "den-hermes-runner", "project_id": "den-hermes-bridge", "role": "runner"},
        "source": {"source_kind": "channel_message", "source_id": "789", "context_link": "den://project/den-hermes-bridge/task/1403"},
        "message": {"summary": "Wake runner for channel mention", "reason": "explicit_mention", "priority": 2},
    }
    base.update(overrides)
    return base


def active_binding(**overrides):
    base = {
        "id": 77,
        "project_id": "den-hermes-bridge",
        "agent_identity": "den-hermes-runner",
        "role": "runner",
        "transport_kind": "hermes_profile",
        "instance_id": "hermes:den-k8:den-hermes-runner:gateway-main",
        "profile": "den-hermes-runner",
        "machine": "den-k8",
        "status": "active",
        "metadata": {"profile": "den-hermes-runner", "machine": "den-k8"},
    }
    base.update(overrides)
    return base


def test_known_delivery_resolves_binding_and_wakes_profile_with_envelope():
    tools = RecordingDenTools([active_binding()])
    transport = RecordingHermesTransport()
    bridge = DenChannelsWakeBridge(den_tools=tools, hermes_transport=transport, store=InMemoryWakeStore())

    result = bridge.handle_delivery(delivery())

    assert result == WakeResult(
        status="delivered",
        delivery_request_id=123,
        dedupe_key="channel-message:789:wake:den-hermes-runner",
        correlation_id="corr-789",
        adapter_instance_id="hermes:den-k8:den-hermes-runner:gateway-main",
        session_id="hermes-session-1",
        external_message_id="spool-1",
        diagnostic=None,
    )
    assert tools.last_binding_query == {
        "project_id": "den-hermes-bridge",
        "agent_identity": "den-hermes-runner",
        "role": "runner",
        "status": "active,degraded",
    }
    assert len(transport.wakes) == 1
    envelope = transport.wakes[0]["envelope"]
    assert envelope["type"] == "den_delivery"
    assert envelope["schema_version"] == 1
    assert envelope["delivery_request_id"] == 123
    assert envelope["attempt_id"] == 456
    assert envelope["dedupe_key"] == "channel-message:789:wake:den-hermes-runner"
    assert envelope["correlation_id"] == "corr-789"
    assert envelope["target"]["agent_identity"] == "den-hermes-runner"
    assert envelope["target"]["profile"] == "den-hermes-runner"
    assert envelope["source"]["context_link"] == "den://project/den-hermes-bridge/task/1403"
    assert "Refresh Den state before acting." in envelope["instructions"]
    assert "api_key" not in json.dumps(envelope).lower()


def test_unknown_target_fails_closed_and_posts_agent_stream_diagnostic():
    tools = RecordingDenTools([])
    transport = RecordingHermesTransport()
    bridge = DenChannelsWakeBridge(den_tools=tools, hermes_transport=transport, store=InMemoryWakeStore())

    result = bridge.handle_delivery(delivery())

    assert result.status == "failed"
    assert result.diagnostic == "No active Hermes profile binding matched den-hermes-runner/runner in den-hermes-bridge"
    assert transport.wakes == []
    assert len(tools.agent_stream_messages) == 1
    diagnostic = tools.agent_stream_messages[0]
    assert diagnostic["sender"] == "den-hermes-bridge"
    assert diagnostic["event_type"] == "note"
    assert diagnostic["project_id"] == "den-hermes-bridge"
    assert diagnostic["recipient_agent"] == "den-hermes-runner"
    assert diagnostic["delivery_mode"] == "record_only"
    assert diagnostic["dedup_key"] == "wake-diagnostic:channel-message:789:wake:den-hermes-runner:failed"
    assert diagnostic["metadata"]["delivery_request_id"] == 123
    assert diagnostic["metadata"]["correlation_id"] == "corr-789"


def test_ambiguous_target_fails_closed_without_wake():
    tools = RecordingDenTools([active_binding(instance_id="one"), active_binding(instance_id="two")])
    transport = RecordingHermesTransport()
    bridge = DenChannelsWakeBridge(den_tools=tools, hermes_transport=transport, store=InMemoryWakeStore())

    result = bridge.handle_delivery(delivery())

    assert result.status == "failed"
    assert result.diagnostic.startswith("Ambiguous Hermes profile binding matched den-hermes-runner/runner in den-hermes-bridge: one, two")
    assert "disambiguate" in result.diagnostic
    assert transport.wakes == []
    assert tools.agent_stream_messages[0]["metadata"]["binding_count"] == 2


def test_duplicate_delivery_is_idempotent_and_does_not_launch_second_wake():
    tools = RecordingDenTools([active_binding()])
    transport = RecordingHermesTransport()
    bridge = DenChannelsWakeBridge(den_tools=tools, hermes_transport=transport, store=InMemoryWakeStore())

    first = bridge.handle_delivery(delivery())
    duplicate = bridge.handle_delivery(delivery())

    assert first.status == "delivered"
    assert duplicate.status == "duplicate"
    assert duplicate.session_id == "hermes-session-1"
    assert len(transport.wakes) == 1
    assert tools.agent_stream_messages == []


def test_transport_failure_records_visible_diagnostic_and_no_secret_values():
    class FailingTransport:
        def wake_profile(self, *, binding, envelope):
            raise RuntimeError("provider token sk-secret-123456 failed")

    tools = RecordingDenTools([active_binding()])
    bridge = DenChannelsWakeBridge(den_tools=tools, hermes_transport=FailingTransport(), store=InMemoryWakeStore())

    result = bridge.handle_delivery(delivery())

    assert result.status == "failed"
    assert "[REDACTED]" in result.diagnostic
    assert "sk-secret" not in result.diagnostic
    assert tools.agent_stream_messages[0]["metadata"]["failure_category"] == "hermes_transport_failure"
    assert "sk-secret" not in json.dumps(tools.agent_stream_messages[0])


def test_envelope_recursively_redacts_secret_keys_and_values_from_delivery_fields():
    tools = RecordingDenTools([active_binding()])
    transport = RecordingHermesTransport()
    bridge = DenChannelsWakeBridge(den_tools=tools, hermes_transport=transport, store=InMemoryWakeStore())

    bridge.handle_delivery(
        delivery(
            target={
                "agent_identity": "den-hermes-runner",
                "project_id": "den-hermes-bridge",
                "role": "runner",
                "api_key": "sk-target-secret-123456",
            },
            source={
                "source_kind": "channel_message",
                "source_id": "789",
                "context_link": "den://project/den-hermes-bridge/task/1403",
                "authorization": "Bearer sk-source-secret-123456",
            },
            message={
                "summary": "Wake runner",
                "token": "sk-message-secret-123456",
                "nested": {"password": "sk-nested-secret-123456"},
            },
        )
    )

    envelope_json = json.dumps(transport.wakes[0]["envelope"])
    assert "sk-target" not in envelope_json
    assert "sk-source" not in envelope_json
    assert "sk-message" not in envelope_json
    assert "sk-nested" not in envelope_json
    assert "api_key" not in envelope_json.lower()
    assert "authorization" not in envelope_json.lower()
    assert "token" not in envelope_json.lower()
    assert "password" not in envelope_json.lower()
    assert "[REDACTED]" in envelope_json


def test_secretish_keys_redact_opaque_values_and_bearer_credentials():
    tools = RecordingDenTools([active_binding()])
    transport = RecordingHermesTransport()
    bridge = DenChannelsWakeBridge(den_tools=tools, hermes_transport=transport, store=InMemoryWakeStore())

    bridge.handle_delivery(
        delivery(
            message={"token": "opaque-value-that-does-not-match-pattern"},
            source={"source_kind": "channel_message", "authorization": "Bearer abc.def.ghi"},
        )
    )

    envelope_json = json.dumps(transport.wakes[0]["envelope"])
    assert "opaque-value" not in envelope_json
    assert "abc.def.ghi" not in envelope_json
    assert "Bearer" not in envelope_json
    assert envelope_json.count("[REDACTED]") >= 2


def test_binding_without_profile_fails_closed():
    tools = RecordingDenTools([active_binding(profile="", metadata={})])
    transport = RecordingHermesTransport()
    bridge = DenChannelsWakeBridge(den_tools=tools, hermes_transport=transport, store=InMemoryWakeStore())

    result = bridge.handle_delivery(delivery())

    assert result.status == "failed"
    assert result.diagnostic == "Hermes profile binding hermes:den-k8:den-hermes-runner:gateway-main has no profile"
    assert transport.wakes == []


def test_core_binding_with_json_metadata_resolves_profile():
    tools = RecordingDenTools(
        [
            active_binding(
                profile=None,
                metadata=json.dumps({"profile": "den-hermes-runner", "machine": "den-k8plus"}),
                instance_id="den-k8plus:den-hermes-runner:coder:canary",
            )
        ]
    )
    transport = RecordingHermesTransport()
    bridge = DenChannelsWakeBridge(den_tools=tools, hermes_transport=transport, store=InMemoryWakeStore())

    result = bridge.handle_delivery(delivery())

    assert result.status == "delivered"
    assert transport.wakes[0]["envelope"]["target"]["profile"] == "den-hermes-runner"
    assert result.adapter_instance_id == "den-k8plus:den-hermes-runner:coder:canary"


class FakePopen:
    def __init__(self, command, *, cwd, env, stdout, stderr, text, start_new_session):
        self.command = command
        self.cwd = cwd
        self.env = env
        self.stdout = stdout
        self.stderr = stderr
        self.text = text
        self.start_new_session = start_new_session
        self.pid = 4242
        self.wait_calls = []

    def poll(self):
        return None

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return 0


def test_spawned_hermes_profile_transport_launches_profile_from_runtime_registry(tmp_path):
    registry = tmp_path / "runtimes.yaml"
    registry.write_text(
        """
        schema_version: 1
        registry_id: test-registry
        defaults:
          substrate: spawned_hermes
          hermes_binary: /bin/hermes-test
          run_root: RUN_ROOT
          artifact_filename: completion.json
          log_filename: worker.log
          timeout_seconds: 300
          toolsets: [terminal]
          workdir: WORKDIR
          preflight: {enabled: false}
        roles:
          coder:
            runtime_id: coder-primary
            profile: den-hermes-runner
            provider: test-provider
            model: test-model
            toolsets: [terminal, file]
            timeout_seconds: 600
            launch: {source: den-channels-wake, extra_args: []}
          reviewer:
            runtime_id: reviewer-primary
            profile: den-hermes-runner
            provider: test-provider
            model: test-model
            toolsets: [terminal]
            timeout_seconds: 600
            launch: {source: den-worker, extra_args: []}
          validator:
            runtime_id: validator-primary
            profile: den-hermes-runner
            provider: test-provider
            model: test-model
            toolsets: [terminal]
            timeout_seconds: 600
            launch: {source: den-worker, extra_args: []}
          drift_checker:
            runtime_id: drift-checker-primary
            profile: den-hermes-runner
            provider: test-provider
            model: test-model
            toolsets: [terminal]
            timeout_seconds: 600
            launch: {source: den-worker, extra_args: []}
          packet_auditor:
            runtime_id: packet-auditor-primary
            profile: den-hermes-runner
            provider: test-provider
            model: test-model
            toolsets: [terminal]
            timeout_seconds: 600
            launch: {source: den-worker, extra_args: []}
          project_orchestrator:
            runtime_id: project-orchestrator-primary
            profile: spawned-orchestrator
            provider: test-provider
            model: test-model
            toolsets: [terminal]
            timeout_seconds: 600
            launch: {source: den-project-orchestrator, extra_args: []}
            lease_kind: project_orchestrator
        role_aliases:
          orchestrator: project_orchestrator
          pooled_orchestrator: project_orchestrator
        """.replace("RUN_ROOT", str(tmp_path / "runs")).replace("WORKDIR", str(tmp_path))
    )
    launches = []

    def popen_factory(*args, **kwargs):
        proc = FakePopen(*args, **kwargs)
        launches.append(proc)
        return proc

    transport = SpawnedHermesProfileWakeTransport(
        runtime_registry_path=registry,
        popen_factory=popen_factory,
        run_id_factory=lambda: "wake-run-1",
    )

    result = transport.wake_profile(binding=active_binding(role="coder"), envelope={"type": "den_delivery", "delivery_request_id": 123})

    proc = launches[0]
    assert result["session_id"] == "wake-run-1"
    assert result["external_message_id"].endswith("/wake-run-1/envelope.json")
    assert proc.command[:6] == ["/bin/hermes-test", "--profile", "den-hermes-runner", "chat", "--provider", "test-provider"]
    assert "--model" in proc.command
    assert "test-model" in proc.command
    assert "--toolsets" in proc.command
    assert "terminal,file" in proc.command
    assert "--source" in proc.command
    assert "den-channels-wake" in proc.command
    assert proc.cwd == str(tmp_path)
    assert proc.start_new_session is True
    assert proc.env["DEN_DELIVERY_REQUEST_ID"] == "123"
    envelope_path = tmp_path / "runs" / "wake-run-1" / "envelope.json"
    assert json.loads(envelope_path.read_text())["delivery_request_id"] == 123


def test_spawned_transport_falls_back_to_bound_profile_for_non_worker_runner_roles(tmp_path, monkeypatch):
    monkeypatch.setenv("DEN_HERMES_BINARY", "/bin/hermes-direct")
    monkeypatch.setenv("DEN_HERMES_WAKE_RUN_ROOT", str(tmp_path / "direct-runs"))
    launches = []

    def popen_factory(*args, **kwargs):
        proc = FakePopen(*args, **kwargs)
        launches.append(proc)
        return proc

    transport = SpawnedHermesProfileWakeTransport(
        runtime_registry_path=tmp_path / "missing-runtime.yaml",
        popen_factory=popen_factory,
        run_id_factory=lambda: "direct-wake-1",
    )

    result = transport.wake_profile(binding=active_binding(role="runner"), envelope={"type": "den_delivery", "delivery_request_id": 321})

    proc = launches[0]
    assert result["session_id"] == "direct-wake-1"
    assert result["runtime_id"] == "direct-profile-wake"
    assert proc.command[:4] == ["/bin/hermes-direct", "--profile", "den-hermes-runner", "chat"]
    assert "--provider" not in proc.command
    assert "--model" not in proc.command
    assert proc.env["DEN_HERMES_PROFILE"] == "den-hermes-runner"
    assert json.loads((tmp_path / "direct-runs" / "direct-wake-1" / "envelope.json").read_text())["delivery_request_id"] == 321

def test_binding_resolver_enforces_exact_project_agent_role_status_locally():
    tools = RecordingDenTools(
        [
            active_binding(project_id="other-project", instance_id="wrong-project"),
            active_binding(agent_identity="other-agent", instance_id="wrong-agent"),
            active_binding(role="planner", instance_id="wrong-role"),
            active_binding(status="inactive", instance_id="inactive"),
            active_binding(instance_id="right"),
        ]
    )
    transport = RecordingHermesTransport()
    bridge = DenChannelsWakeBridge(den_tools=tools, hermes_transport=transport, store=InMemoryWakeStore())

    result = bridge.handle_delivery(delivery())
    assert result.status == "delivered"
    assert result.adapter_instance_id == "right"
    assert len(transport.wakes) == 1


def test_binding_resolver_rejects_bindings_missing_required_identity_fields():
    incomplete = active_binding()
    del incomplete["project_id"]
    incomplete_agent = active_binding(instance_id="missing-agent")
    del incomplete_agent["agent_identity"]
    incomplete_status = active_binding(instance_id="missing-status")
    del incomplete_status["status"]
    tools = RecordingDenTools([incomplete, incomplete_agent, incomplete_status])
    transport = RecordingHermesTransport()
    bridge = DenChannelsWakeBridge(den_tools=tools, hermes_transport=transport, store=InMemoryWakeStore())

    result = bridge.handle_delivery(delivery())

    assert result.status == "failed"
    assert result.diagnostic == "No active Hermes profile binding matched den-hermes-runner/runner in den-hermes-bridge"
    assert transport.wakes == []


def test_delivery_target_requires_explicit_role_before_wake():
    tools = RecordingDenTools([active_binding()])
    transport = RecordingHermesTransport()
    bridge = DenChannelsWakeBridge(den_tools=tools, hermes_transport=transport, store=InMemoryWakeStore())
    event = delivery(target={"agent_identity": "den-hermes-runner", "project_id": "den-hermes-bridge"})

    result = bridge.handle_delivery(event)

    assert result.status == "failed"
    assert result.diagnostic == "Delivery target is missing required role for den-hermes-runner in den-hermes-bridge"
    assert transport.wakes == []



def test_project_message_reply_preserves_source_references_and_dedupes_visible_retries():
    tools = RecordingDenTools([])
    bridge = DenChannelsResponseBridge(den_tools=tools, store=InMemoryWakeStore())
    event = delivery(
        response_target={"kind": "project_message", "project_id": "den-hermes-bridge", "task_id": 1404, "thread_id": 5878}
    )

    first = bridge.post_reply(event, body="Hermes handled the channel wake.", run_id="run-1404")
    duplicate = bridge.post_reply(event, body="Hermes handled the channel wake again.", run_id="run-1404")

    assert first.status == "posted"
    assert duplicate.status == "duplicate"
    assert len(tools.project_messages) == 1
    posted = tools.project_messages[0]
    assert posted["project_id"] == "den-hermes-bridge"
    assert posted["task_id"] == 1404
    assert posted["thread_id"] == 5878
    assert posted["sender"] == "den-hermes-bridge"
    assert posted["content"] == "Hermes handled the channel wake."
    assert posted["metadata"]["source"]["source_id"] == "789"
    assert posted["metadata"]["delivery_request_id"] == 123
    assert posted["metadata"]["run_id"] == "run-1404"
    assert posted["metadata"]["correlation_id"] == "corr-789"


def test_project_message_reply_rejects_cross_project_response_target():
    tools = RecordingDenTools([])
    bridge = DenChannelsResponseBridge(den_tools=tools, store=InMemoryWakeStore())
    event = delivery(response_target={"kind": "project_message", "project_id": "other-project", "task_id": 1404})

    result = bridge.post_reply(event, body="do not cross-post", run_id="run-1404")

    assert result.status == "failed"
    assert "outside delivery project" in result.diagnostic
    assert tools.project_messages == []


def test_project_message_reply_dedupes_across_persistent_store_retries(tmp_path):
    tools = RecordingDenTools([])
    store_path = tmp_path / "wake-store.json"
    event = delivery(response_target={"kind": "project_message", "project_id": "den-hermes-bridge", "task_id": 1404})

    first_bridge = DenChannelsResponseBridge(den_tools=tools, store=JsonFileWakeStore(store_path))

    first = first_bridge.post_reply(event, body="first visible reply", run_id="run-1404")
    second_bridge = DenChannelsResponseBridge(den_tools=tools, store=JsonFileWakeStore(store_path))
    duplicate = second_bridge.post_reply(event, body="second visible reply", run_id="run-1404")

    assert first.status == "posted"
    assert duplicate.status == "duplicate"
    assert len(tools.project_messages) == 1
    assert Path(store_path).exists()


def test_lifecycle_events_go_to_agent_stream_with_debug_visibility_for_noisy_events():
    tools = RecordingDenTools([])
    bridge = DenChannelsResponseBridge(den_tools=tools, store=InMemoryWakeStore())
    event = delivery()

    bridge.emit_lifecycle(event, lifecycle_event="received", run_id="run-1404")
    bridge.emit_lifecycle(event, lifecycle_event="completed", run_id="run-1404")

    assert len(tools.agent_stream_messages) == 2
    received = tools.agent_stream_messages[0]
    completed = tools.agent_stream_messages[1]
    assert received["sender"] == "den-hermes-bridge"
    assert received["event_type"] == "note"
    assert received["recipient_agent"] == "den-hermes-runner"
    assert received["recipient_role"] == "runner"
    assert received["delivery_mode"] == "record_only"
    assert received["metadata"]["stream_kind"] == "ops"
    assert received["metadata"]["event_visibility"] == "debug"
    assert received["metadata"]["lifecycle_event"] == "received"
    assert completed["delivery_mode"] == "notify"
    assert completed["metadata"]["event_visibility"] == "summary"
    assert completed["metadata"]["source"]["context_link"] == "den://project/den-hermes-bridge/task/1403"


def test_response_bridge_supports_agent_stream_user_notification_and_channel_targets():
    tools = RecordingDenTools([])
    gateway = RecordingGatewayClient()
    bridge = DenChannelsResponseBridge(den_tools=tools, gateway_client=gateway, store=InMemoryWakeStore())

    stream_result = bridge.post_reply(
        delivery(response_target={"kind": "agent_stream", "event_type": "answer"}),
        body="stream reply",
        run_id="stream-run",
    )
    notification_result = bridge.post_reply(
        delivery(response_target={"kind": "user_notification", "urgency": "high"}, dedupe_key="notify-key"),
        body="notification reply",
        run_id="notify-run",
    )
    channel_result = bridge.post_reply(
        delivery(response_target={"kind": "channel_message", "channel_id": "telegram:-100:55"}, dedupe_key="channel-key"),
        body="channel reply",
        run_id="channel-run",
    )

    assert stream_result.status == "posted"
    assert notification_result.status == "posted"
    assert channel_result.status == "posted"
    assert tools.agent_stream_messages[-1]["event_type"] == "answer"
    assert tools.user_notifications[0]["urgency"] == "high"
    assert gateway.channel_messages[0]["channel_id"] == "telegram:-100:55"


def test_end_to_end_wake_then_visible_reply_smoke():
    tools = RecordingDenTools([active_binding()])
    transport = RecordingHermesTransport()
    store = InMemoryWakeStore()
    wake_bridge = DenChannelsWakeBridge(den_tools=tools, hermes_transport=transport, store=store)
    response_bridge = DenChannelsResponseBridge(den_tools=tools, store=store)
    event = delivery(response_target={"kind": "project_message", "project_id": "den-hermes-bridge", "task_id": 1404})

    wake = wake_bridge.handle_delivery(event)
    reply = response_bridge.post_reply(event, body="visible reply from Hermes", run_id=wake.session_id)

    assert wake.status == "delivered"
    assert reply.status == "posted"
    assert transport.wakes[0]["envelope"]["delivery_request_id"] == 123
    assert tools.project_messages[0]["content"] == "visible reply from Hermes"


# ---------------------------------------------------------------------------
# Spawned role pool-member identity tests
# ---------------------------------------------------------------------------


def spawned_binding(**overrides):
    """Create a spawned-role binding with pool_member_id."""
    base = {
        "id": 100,
        "project_id": "den-hermes-bridge",
        "agent_identity": "spawned-coder",
        "role": "coder",
        "transport_kind": "hermes_profile",
        "instance_id": "hermes:den-k8:spawned-coder:wake-abc123",
        "pool_member_id": "pool-coder-01",
        "profile": "spawned-coder",
        "machine": "den-k8",
        "status": "active",
        "metadata": {"profile": "spawned-coder", "pool_member_id": "pool-coder-01", "machine": "den-k8"},
    }
    base.update(overrides)
    return base


def spawned_delivery(**overrides):
    """Create a delivery targeting a spawned-coder with concrete identity."""
    base = {
        "delivery_request_id": 456,
        "attempt_id": 789,
        "delivery_mode": "wake",
        "dedupe_key": "pool-assign:999:wake:spawned-coder",
        "correlation_id": "corr-pool-999",
        "target": {
            "agent_identity": "spawned-coder",
            "project_id": "den-hermes-bridge",
            "role": "coder",
            "pool_member_id": "pool-coder-01",
            "concrete_identity": "pool-coder-01",
        },
        "source": {"source_kind": "worker_pool", "source_id": "assign-001"},
        "message": {"summary": "Wake pool coder for assignment", "reason": "pool_assignment"},
    }
    base.update(overrides)
    return base


def test_shared_profile_ambiguous_without_concrete_target_fails_closed():
    """Two spawned-coder bindings and no concrete target → fail closed (ambiguous)."""
    tools = RecordingDenTools([
        spawned_binding(instance_id="pool-coder-01", pool_member_id="pool-coder-01"),
        spawned_binding(instance_id="pool-coder-02", pool_member_id="pool-coder-02"),
    ])
    transport = RecordingHermesTransport()
    bridge = DenChannelsWakeBridge(den_tools=tools, hermes_transport=transport, store=InMemoryWakeStore())

    delivery_no_concrete = spawned_delivery()
    delivery_no_concrete["target"] = {
        "agent_identity": "spawned-coder",
        "project_id": "den-hermes-bridge",
        "role": "coder",
    }

    result = bridge.handle_delivery(delivery_no_concrete)

    assert result.status == "failed"
    assert "ambiguous" in (result.diagnostic or "").lower()
    assert "disambiguate" in (result.diagnostic or "")
    assert transport.wakes == []
    assert tools.agent_stream_messages[0]["metadata"]["failure_category"] == "ambiguous_binding"


def test_shared_profile_with_pool_member_id_resolves_concrete_binding():
    """Two spawned-coder bindings with pool_member_id target → resolves to matching one."""
    tools = RecordingDenTools([
        spawned_binding(instance_id="pool-coder-01", pool_member_id="pool-coder-01"),
        spawned_binding(instance_id="pool-coder-02", pool_member_id="pool-coder-02"),
    ])
    transport = RecordingHermesTransport()
    bridge = DenChannelsWakeBridge(den_tools=tools, hermes_transport=transport, store=InMemoryWakeStore())

    result = bridge.handle_delivery(spawned_delivery())

    assert result.status == "delivered"
    assert result.adapter_instance_id == "pool-coder-01"
    assert len(transport.wakes) == 1
    envelope = transport.wakes[0]["envelope"]
    assert envelope["target"]["pool_member_id"] == "pool-coder-01"
    assert envelope["target"]["profile_identity"] == "spawned-coder"
    assert envelope["target"]["worker_identity"] == "pool-coder-01"


def test_shared_profile_with_instance_id_resolves_concrete_binding():
    """Two spawned-coder bindings with agent_instance_id target → resolves to matching one."""
    tools = RecordingDenTools([
        spawned_binding(instance_id="hermes:den-k8:spawned-coder:wake-abc123", pool_member_id="pool-coder-01"),
        spawned_binding(instance_id="hermes:den-k8:spawned-coder:wake-def456", pool_member_id="pool-coder-02"),
    ])
    transport = RecordingHermesTransport()
    bridge = DenChannelsWakeBridge(den_tools=tools, hermes_transport=transport, store=InMemoryWakeStore())

    result = bridge.handle_delivery(
        spawned_delivery(
            target={
                "agent_identity": "spawned-coder",
                "project_id": "den-hermes-bridge",
                "role": "coder",
                "agent_instance_id": "hermes:den-k8:spawned-coder:wake-def456",
            },
        )
    )

    assert result.status == "delivered"
    assert result.adapter_instance_id == "hermes:den-k8:spawned-coder:wake-def456"
    assert len(transport.wakes) == 1


def test_shared_profile_concrete_target_no_match_fails_closed():
    """Concrete target that doesn't match any binding → fail closed with diagnostic."""
    tools = RecordingDenTools([
        spawned_binding(instance_id="pool-coder-01", pool_member_id="pool-coder-01"),
        spawned_binding(instance_id="pool-coder-02", pool_member_id="pool-coder-02"),
    ])
    transport = RecordingHermesTransport()
    bridge = DenChannelsWakeBridge(den_tools=tools, hermes_transport=transport, store=InMemoryWakeStore())

    result = bridge.handle_delivery(
        spawned_delivery(
            target={
                "agent_identity": "spawned-coder",
                "project_id": "den-hermes-bridge",
                "role": "coder",
                "pool_member_id": "pool-coder-99",
            },
        )
    )

    assert result.status == "failed"
    assert ("concrete identity" in (result.diagnostic or "").lower()
            or "no active binding" in (result.diagnostic or "").lower())
    assert "pool-coder-99" in (result.diagnostic or "")
    assert transport.wakes == []
    assert tools.agent_stream_messages[0]["metadata"]["failure_category"] == "concrete_binding_not_found"


def test_shared_profile_concrete_target_matching_multiple_bindings_fails_closed():
    """Concrete target must select exactly one active binding, not first-match silently."""
    tools = RecordingDenTools([
        spawned_binding(instance_id="pool-coder-01a", pool_member_id="pool-coder-01"),
        spawned_binding(instance_id="pool-coder-01b", pool_member_id="pool-coder-01"),
    ])
    transport = RecordingHermesTransport()
    bridge = DenChannelsWakeBridge(den_tools=tools, hermes_transport=transport, store=InMemoryWakeStore())

    result = bridge.handle_delivery(spawned_delivery())

    assert result.status == "failed"
    assert "matched multiple active bindings" in (result.diagnostic or "")
    assert transport.wakes == []
    assert tools.agent_stream_messages[0]["metadata"]["failure_category"] == "ambiguous_concrete_binding"
    assert tools.agent_stream_messages[0]["metadata"]["binding_count"] == 2


def test_envelope_includes_pool_member_id_and_identity_fields_when_binding_has_pool_member():
    """Delivery envelope should include pool_member_id, profile_identity, and worker_identity."""
    tools = RecordingDenTools([spawned_binding()])
    transport = RecordingHermesTransport()
    bridge = DenChannelsWakeBridge(den_tools=tools, hermes_transport=transport, store=InMemoryWakeStore())

    result = bridge.handle_delivery(spawned_delivery())

    assert result.status == "delivered"
    envelope = transport.wakes[0]["envelope"]
    target = envelope["target"]
    assert target["pool_member_id"] == "pool-coder-01"
    assert target["profile_identity"] == "spawned-coder"
    assert target["worker_identity"] == "pool-coder-01"
    assert target["adapter_instance_id"] == "hermes:den-k8:spawned-coder:wake-abc123"
    assert target["profile"] == "spawned-coder"


def test_envelope_omits_pool_member_id_when_binding_lacks_one():
    """Delivery envelope should not include pool_member_id if binding doesn't have one."""
    tools = RecordingDenTools([active_binding()])
    transport = RecordingHermesTransport()
    bridge = DenChannelsWakeBridge(den_tools=tools, hermes_transport=transport, store=InMemoryWakeStore())

    result = bridge.handle_delivery(delivery())

    assert result.status == "delivered"
    envelope = transport.wakes[0]["envelope"]
    target = envelope["target"]
    assert "pool_member_id" not in target
    assert target["profile_identity"] == "den-hermes-runner"
    assert target["worker_identity"] == "hermes:den-k8:den-hermes-runner:gateway-main"


def test_delivery_with_concrete_identity_resolves_single_binding_no_ambiguity():
    """Single binding with concrete identity should behave identically to existing single-binding path."""
    tools = RecordingDenTools([spawned_binding()])
    transport = RecordingHermesTransport()
    bridge = DenChannelsWakeBridge(den_tools=tools, hermes_transport=transport, store=InMemoryWakeStore())

    result = bridge.handle_delivery(spawned_delivery())

    assert result.status == "delivered"
    assert len(transport.wakes) == 1
    assert result.adapter_instance_id == "hermes:den-k8:spawned-coder:wake-abc123"


def test_transport_env_includes_pool_member_id():
    """Verify that SpawnedHermesProfileWakeTransport sets DEN_HERMES_POOL_MEMBER_ID."""
    registry_path = Path(__file__).parent / "test_registry_runtimes.yaml"
    if not registry_path.exists():
        registry_path = Path("/home/agents/runtime/spawned-hermes-runtimes.yaml")

    launches = []

    def popen_factory(*args, **kwargs):
        proc = FakePopen(*args, **kwargs)
        launches.append(proc)
        return proc

    transport = SpawnedHermesProfileWakeTransport(
        runtime_registry_path=registry_path,
        popen_factory=popen_factory,
        run_id_factory=lambda: "pool-wake-1",
    )

    # Use a runner-role binding that bypasses the worker registry fallback path
    binding = spawned_binding(role="runner", profile="den-hermes-runner", agent_identity="den-hermes-runner")
    transport.wake_profile(binding=binding, envelope={"type": "den_delivery", "delivery_request_id": 123})

    assert len(launches) == 1
    env = launches[0].env
    assert env.get("DEN_HERMES_POOL_MEMBER_ID") == "pool-coder-01"


def test_transport_env_pool_member_id_empty_when_binding_lacks_one():
    """DEN_HERMES_POOL_MEMBER_ID should be empty string when binding has no pool_member_id."""
    registry_path = Path("/home/agents/runtime/spawned-hermes-runtimes.yaml")

    launches = []

    def popen_factory(*args, **kwargs):
        proc = FakePopen(*args, **kwargs)
        launches.append(proc)
        return proc

    transport = SpawnedHermesProfileWakeTransport(
        runtime_registry_path=registry_path,
        popen_factory=popen_factory,
        run_id_factory=lambda: "plain-wake-1",
    )

    binding = active_binding()
    transport.wake_profile(binding=binding, envelope={"type": "den_delivery", "delivery_request_id": 123})

    assert len(launches) == 1
    env = launches[0].env
    assert env.get("DEN_HERMES_POOL_MEMBER_ID") == ""

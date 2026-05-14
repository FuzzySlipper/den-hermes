import json
from pathlib import Path

from den_hermes.channels_bridge import DenChannelsResponseBridge, DenChannelsWakeBridge, InMemoryWakeStore, JsonFileWakeStore, WakeResult


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
    assert result.diagnostic == "Ambiguous Hermes profile binding matched den-hermes-runner/runner in den-hermes-bridge: one, two"
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

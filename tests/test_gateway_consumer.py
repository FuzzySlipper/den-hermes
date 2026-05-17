from __future__ import annotations

from den_hermes.gateway_consumer import GatewayDeliveryConsumer


class RecordingGatewayClient:
    def __init__(self):
        self.bindings = [
            {
                "agentIdentity": "den-channels-runner",
                "projectId": "den-channels",
                "role": "runner",
                "adapterKind": "hermes_profile",
                "adapterInstanceId": "den-k8plus:den-channels-runner:runner:systemd-test",
                "status": "active",
                "isStale": False,
                "metadataJson": '{"profile":"den-channels-runner"}',
            }
        ]
        self.claims = []
        self.heartbeats = []
        self.delivered = []
        self.failed = []

    def list_binding_snapshots(self):
        return self.bindings

    def upsert_adapter_binding(self, payload):
        self.heartbeats.append(payload)
        return {"binding_id": 101}

    def claim_deliveries(self, request):
        self.claims.append(request)
        assert request["adapter_kind"] == "hermes_profile"
        assert request["adapter_instance_id"] == "den-k8plus:den-channels-runner:runner:systemd-test"
        assert request["project_id"] == "den-channels"
        assert request["agent_identity"] == "den-channels-runner"
        assert request["role"] == "runner"
        assert request["accepted_delivery_modes"] == ["wake", "notify"]
        return [
            {
                "delivery_request_id": 44,
                "attempt_id": 55,
                "target_type": "agent",
                "target_identity": "den-channels-runner",
                "project_id": "den-channels",
                "delivery_mode": "wake",
                "source_kind": "channel_message",
                "source_id": "4",
                "source_project_id": "den-channels",
                "context_summary": "Testing a message",
                "context_link": "den://channel/2/message/4",
                "metadata_json": '{"source":"channels","wake_policy":"all_human_messages"}',
                "dedupe_key": "channel-message:4:agent:den-channels-runner",
            }
        ]

    def mark_delivered(self, delivery_request_id, payload):
        self.delivered.append((delivery_request_id, payload))

    def mark_failed(self, delivery_request_id, payload):
        self.failed.append((delivery_request_id, payload))


class RecordingTransport:
    def __init__(self):
        self.wakes = []

    def wake_profile(self, *, binding, envelope):
        self.wakes.append({"binding": binding, "envelope": envelope})
        return {"session_id": "wake-run-1", "external_message_id": "/tmp/envelope.json"}


def test_consumer_claims_gateway_deliveries_wakes_profile_and_marks_delivered():
    gateway = RecordingGatewayClient()
    transport = RecordingTransport()
    consumer = GatewayDeliveryConsumer(gateway_client=gateway, hermes_transport=transport)

    result = consumer.poll_once()

    assert result == {"bindings_seen": 1, "deliveries_claimed": 1, "delivered": 1, "failed": 0}
    assert len(transport.wakes) == 1
    assert gateway.heartbeats[0]["adapter_kind"] == "hermes_profile"
    assert gateway.heartbeats[0]["adapter_instance_id"] == "den-k8plus:den-channels-runner:runner:systemd-test"
    assert gateway.heartbeats[0]["agent_identity"] == "den-channels-runner"
    wake = transport.wakes[0]
    assert wake["binding"]["role"] == "runner"
    assert wake["binding"]["profile"] == "den-channels-runner"
    assert wake["envelope"]["delivery_request_id"] == 44
    assert wake["envelope"]["attempt_id"] == 55
    assert wake["envelope"]["target"] == {
        "project_id": "den-channels",
        "agent_identity": "den-channels-runner",
        "role": "runner",
        "profile": "den-channels-runner",
        "adapter_instance_id": "den-k8plus:den-channels-runner:runner:systemd-test",
    }
    assert wake["envelope"]["reply"]["source_kind"] == "external_adapter_message"
    assert wake["envelope"]["reply"]["source_id"] == "44"
    assert gateway.delivered == [
        (
            44,
            {
                "attempt_id": 55,
                "adapter_kind": "hermes_profile",
                "adapter_instance_id": "den-k8plus:den-channels-runner:runner:systemd-test",
                "external_message_id": "/tmp/envelope.json",
                "session_id": "wake-run-1",
            },
        )
    ]
    assert gateway.failed == []

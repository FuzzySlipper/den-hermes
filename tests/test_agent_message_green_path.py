from den_hermes.agent_message import AgentMessageResult, DenChannelsAgentMessenger


class RecordingChannelsTools:
    def __init__(self, memberships_by_key):
        self.memberships_by_key = memberships_by_key
        self.membership_queries = []
        self.sent = []
        self.messages = {321: {"id": 321, "deliveryRequestId": 654, "sourceKind": "direct_agent_message"}}
        self.send_response = {
            "status": "ok",
            "delivery_intent_id": 321,
            "message": {"id": 321},
        }
        self.events = [{"id": 9, "eventType": "recorded_pending_claim", "messageId": 321}]

    def den_channels_get_memberships(self, **kwargs):
        self.membership_queries.append(kwargs)
        key = ("channel", kwargs["channel_id"]) if "channel_id" in kwargs else ("project", kwargs["project_id"])
        return {"status": "ok", "memberships": self.memberships_by_key[key]}

    def den_channels_send_direct_agent_message(self, **kwargs):
        self.sent.append(kwargs)
        return self.send_response

    def den_channels_get_message(self, message_id):
        return {"message": self.messages[message_id]}

    def den_channels_get_events(self, **kwargs):
        self.last_events_query = kwargs
        return {"events": self.events}


def memberships(channel_id=5, slug="project-den-hermes-bridge", project_id: str | None = "den-hermes-bridge", members=None):
    return {
        "channelId": channel_id,
        "channelSlug": slug,
        "channelKind": "project_default" if project_id else "system",
        "projectId": project_id,
        "members": members
        or [
            {
                "memberIdentity": "den-mcp-runner",
                "memberType": "agent",
                "membershipStatus": "active",
                "wakePolicy": "mentions_only",
                "canSend": True,
            }
        ],
    }


def test_send_agent_message_resolves_explicit_channel_and_returns_evidence():
    tools = RecordingChannelsTools({("channel", 5): memberships()})
    messenger = DenChannelsAgentMessenger(tools=tools, base_url="http://den.test")

    result = messenger.send_agent_message(
        member_identity="den-mcp-runner",
        body="Wake runner with evidence.",
        channel_id=5,
        sender_identity="den-mcp-planner",
    )

    assert result.status == "sent"
    assert result.channel_id == 5
    assert result.channel_slug == "project-den-hermes-bridge"
    assert result.message_id == 321
    assert result.delivery_intent_id == 321
    assert result.delivery_intent_url == "http://den.test/v1/delivery/intents/321"
    assert result.delivery_intents_url == "http://den.test/v1/delivery/intents"
    assert result.delivery_status == "ok"
    assert not hasattr(tools, "last_events_query")
    assert tools.sent == [
        {
            "channel_id": 5,
            "member_identity": "den-mcp-runner",
            "body": "Wake runner with evidence.",
            "sender_identity": "den-mcp-planner",
        }
    ]


def test_send_agent_message_resolves_project_default_channel():
    tools = RecordingChannelsTools({("project", "den-core"): memberships(channel_id=3, slug="project-den-core", project_id="den-core")})
    messenger = DenChannelsAgentMessenger(tools=tools)

    result = messenger.send_agent_message(member_identity="den-mcp-runner", body="Project wake", project_id="den-core")

    assert result.status == "sent"
    assert result.channel_id == 3
    assert tools.membership_queries == [{"project_id": "den-core"}]
    assert tools.sent[0]["channel_id"] == 3


def test_send_agent_message_falls_back_to_agent_commons_when_project_unknown():
    tools = RecordingChannelsTools(
        {("channel", 21): memberships(channel_id=21, slug="agent-commons", project_id=None)}
    )
    messenger = DenChannelsAgentMessenger(tools=tools)

    result = messenger.send_agent_message(member_identity="den-mcp-runner", body="Global wake")

    assert result.status == "sent"
    assert result.channel_id == 21
    assert result.channel_slug == "agent-commons"
    assert tools.membership_queries == [{"channel_id": 21}]


def test_send_agent_message_refuses_non_member_without_sending():
    tools = RecordingChannelsTools({("channel", 21): memberships(channel_id=21, slug="agent-commons", project_id=None, members=[])})
    messenger = DenChannelsAgentMessenger(tools=tools)

    result = messenger.send_agent_message(member_identity="missing-agent", body="Should not send")

    assert result == AgentMessageResult(
        status="not_sent",
        member_identity="missing-agent",
        channel_id=21,
        channel_slug="agent-commons",
        channel_kind="system",
        project_id=None,
        diagnostic="missing-agent is not an active agent member of channel 21 (agent-commons)",
    )
    assert tools.sent == []


def test_send_agent_message_prefers_explicit_channel_over_project():
    tools = RecordingChannelsTools({("channel", 7): memberships(channel_id=7, slug="explicit-channel", project_id="den-core")})
    messenger = DenChannelsAgentMessenger(tools=tools)

    result = messenger.send_agent_message(
        member_identity="den-mcp-runner",
        body="Explicit channel wins",
        channel_id=7,
        project_id="ignored-project",
    )

    assert result.status == "sent"
    assert tools.membership_queries == [{"channel_id": 7}]
    assert result.channel_id == 7


def test_send_agent_message_does_not_use_legacy_direct_event_readback_without_delivery_id():
    tools = RecordingChannelsTools({
        ("channel", 5): memberships(
            members=[
                {
                    "memberIdentity": "spawned-coder",
                    "memberType": "agent",
                    "membershipStatus": "active",
                    "wakePolicy": "mentions_only",
                    "canSend": True,
                }
            ]
        )
    })
    tools.messages = {321: {"id": 321, "sourceKind": "wake_event"}}
    tools.send_response = {"status": "ok", "message": {"id": 321}, "message_id": 321}
    tools.events = [
        {
            "id": 321,
            "messageId": 321,
            "messageKind": "human_text",
            "sourceKind": "wake_event",
            "summary": "Direct agent request to spawned-coder: recorded, pending claim/completion",
            "senderIdentity": "den-mcp-runner",
            "senderType": "user",
        },
        {
            "id": 322,
            "messageKind": "agent_text",
            "senderIdentity": "spawned-coder",
            "senderType": "agent",
            "sourceKind": "gateway_delivery",
            "sourceId": "959",
            "body": "I've read the coder context packet and task.",
        },
    ]
    messenger = DenChannelsAgentMessenger(tools=tools)

    result = messenger.send_agent_message(member_identity="spawned-coder", body="Start", channel_id=5)

    assert result.delivery_request_id is None
    assert result.delivery_status == "ok"
    assert not hasattr(tools, "last_events_query")


def test_send_agent_message_carries_target_project_and_task_in_send_args():
    """When target_project_id and target_task_id are provided, they flow to the direct-agent send args."""
    tools = RecordingChannelsTools({("channel", 5): memberships()})
    messenger = DenChannelsAgentMessenger(tools=tools)

    result = messenger.send_agent_message(
        member_identity="den-mcp-runner",
        body="Review den-core task #1820",
        channel_id=5,
        target_project_id="den-core",
        target_task_id=1820,
        target_assignment_id=63,
    )

    assert result.status == "sent"
    assert result.target_project_id == "den-core"
    assert result.target_task_id == 1820
    assert result.target_assignment_id == 63
    assert result.project_id == "den-hermes-bridge"  # transport channel project
    sent = tools.sent[0]
    assert sent["source_project_id"] == "den-core"
    assert sent["target_task_id"] == 1820
    assert sent["assignment_id"] == "63"


def test_send_agent_message_target_project_differs_from_channel_project():
    """Regression: non-bridge target_project_id must not collapse to channel project."""
    tools = RecordingChannelsTools({("channel", 5): memberships()})
    messenger = DenChannelsAgentMessenger(tools=tools)

    result = messenger.send_agent_message(
        member_identity="den-mcp-runner",
        body="Work on goblinbench task",
        channel_id=5,
        target_project_id="goblinbench",
    )

    assert result.status == "sent"
    assert result.target_project_id == "goblinbench"
    assert result.project_id == "den-hermes-bridge"
    assert result.target_project_id != result.project_id


def test_send_agent_message_without_target_project_preserves_existing_behavior():
    """Without target_project_id, project_id is channel project (backward compat)."""
    tools = RecordingChannelsTools({("channel", 5): memberships()})
    messenger = DenChannelsAgentMessenger(tools=tools)

    result = messenger.send_agent_message(
        member_identity="den-mcp-runner",
        body="Standard wake",
        channel_id=5,
    )

    assert result.status == "sent"
    assert result.target_project_id is None
    assert result.project_id == "den-hermes-bridge"
    sent = tools.sent[0]
    assert "source_project_id" not in sent

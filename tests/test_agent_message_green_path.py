from den_hermes.agent_message import AgentMessageResult, DenChannelsAgentMessenger


class RecordingChannelsTools:
    def __init__(self, memberships_by_key):
        self.memberships_by_key = memberships_by_key
        self.membership_queries = []
        self.sent = []
        self.messages = {321: {"id": 321, "deliveryRequestId": 654, "sourceKind": "direct_agent_message"}}
        self.send_response = {
            "status": "ok",
            "message": {"id": 321, "deliveryRequestId": 654},
            "message_id": 321,
            "delivery_request_id": 654,
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
    assert result.delivery_request_id == 654
    assert result.gateway_message_url == "http://den.test/api/gateway/messages/321"
    assert result.gateway_events_url == "http://den.test/api/gateway/events?channelId=5&afterId=0"
    assert result.delivery_status == "recorded_pending_claim"
    assert tools.last_events_query == {"channel_id": 5, "after_id": 320, "limit": 50}
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


def test_send_agent_message_observes_agent_reply_after_direct_wake_without_delivery_id():
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
    assert result.delivery_status == "agent_reply_posted"
    assert tools.last_events_query == {"channel_id": 5, "after_id": 320, "limit": 50}

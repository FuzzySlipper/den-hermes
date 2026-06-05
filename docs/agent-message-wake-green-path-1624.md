# Agent Message/Wake Green Path (Tasks 1624/1626)

Status: implemented as a Den-owned bridge wrapper and smoke preflight script.

## Standard surface

Normal Hermes/Den agents should use **Den Channels / Gateway direct-agent delivery** when asked to message, DM, ping, or wake another agent.

Do not use `send_agent_stream_message` as the ordinary message/wake primitive. Agent-stream writes remain compatibility/ops telemetry only; they do not provide the canonical Channels/Gateway evidence for a direct-agent wake.

## Wrapper

`den_hermes.agent_message.DenChannelsAgentMessenger.send_agent_message(...)` is the green-path wrapper for code paths that have access to the Den Channels tool surface.

Inputs:

- `member_identity` (required)
- `body` (required)
- `channel_id` (optional)
- `project_id` (optional)
- `sender_identity` (optional)

Resolution order:

1. explicit `channel_id`
2. `project_id` default channel via `den_channels_get_memberships(project_id=...)`
3. Agent Commons fallback (`channel_id=21`, slug `agent-commons`)

The wrapper preflights membership before sending and returns `status="not_sent"` if the target is not an active agent member of the resolved channel. It sends only after membership preflight succeeds.

Evidence returned:

- resolved channel id / slug / kind / project id
- direct-agent channel message id
- delivery/request id when present in send or message payloads
- gateway message URL
- gateway events URL
- best-effort delivery/event status from gateway events
- non-wake diagnostic when not sent

## Smoke/preflight runbook

Non-mutating preflight:

```bash
PYTHONPATH=/home/dev/den-hermes \
  python scripts/smoke_agent_message_green_path.py den-mcp-runner --project-id den-core
```

Expected shape:

```json
{
  "status": "preflight_ok",
  "channelId": 3,
  "channelSlug": "project-den-core",
  "memberIdentity": "den-mcp-runner",
  "membershipStatus": "active",
  "wakePolicy": "mentions_only",
  "directAgentEventsUrl": "http://192.168.1.10:18080/api/direct-agent-events?channelId=3&afterId=0"
}
```

Project-unknown fallback preflight uses Agent Commons:

```bash
PYTHONPATH=/home/dev/den-hermes \
  python scripts/smoke_agent_message_green_path.py den-mcp-runner
```

Non-member negative check:

```bash
PYTHONPATH=/home/dev/den-hermes \
  python scripts/smoke_agent_message_green_path.py missing-agent
```

Expected: exit code `2`, `status="not_sent"`, and no send.

Mutating smoke (only when safe to wake the target):

```bash
PYTHONPATH=/home/dev/den-hermes \
  python scripts/smoke_agent_message_green_path.py den-mcp-runner \
    --project-id den-core \
    --sender-identity den-mcp-planner \
    --send \
    "Direct-agent green-path wake smoke; please acknowledge with evidence."
```

## Current inventory snapshot (2026-05-24)

Live Den Channels membership checks from the Runner environment:

- Agent Commons: channel id `21`, slug `agent-commons`, kind `system`; active members include `den-mcp-planner`, `den-mcp-runner`, `den-hermes-runner`, `den-channels-runner`, `den-desktop-runner`, `sysadmin`, and other project agents. `reviewer` is muted/never.
- `den-hermes-bridge` default channel: channel id `5`, slug `project-den-hermes-bridge`; active member `den-hermes-runner`.
- `den-core` default channel: channel id `3`, slug `project-den-core`; active members `den-mcp-planner` and `den-mcp-runner`.

Profile config scan under `/home/agents/profiles/*/config.yaml` showed the standard Den MCP server configured on normal profiles, but no profile-local `den_channels` platform plugin enabled in `plugins.enabled`. That is acceptable for this wrapper when the Den Channels tool surface is supplied by the calling environment/profile/toolset; rollout work should ensure Planner/Runner/operator profiles receive this wrapper/tool surface through the chosen plugin/toolset/MCP profile path.

Core binding snapshot has an active `den-hermes-runner` Hermes profile binding for `den-hermes-bridge` coder/canary. Treat Core bindings as diagnostics; active Den Channels membership is the preflight gate for the wrapper's send/no-send decision.

## Packaging guidance

- Package this as the standard `send_agent_message`/`wake_agent_via_channels` surface for Planner/Runner/operator-style profiles.
- Keep `send_agent_stream_message` out of normal profiles. If exposed in a break-glass/operator diagnostics profile, describe it as Core agent-stream compatibility/ops telemetry, not the canonical wake path.
- For profile-filtered MCP work (#1620/#1621), add the green-path wrapper to the normal Planner/Runner profile surface and put agent-stream write tools only in legacy/compat/operator diagnostics.

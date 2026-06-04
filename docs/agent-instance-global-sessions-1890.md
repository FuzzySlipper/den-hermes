# Agent Instance Global Sessions (#1890)

## Status

Accepted and implemented in den-hermes only.

## Context

The Den Channels Hermes adapter (#1871) resolved session continuity from
explicit conversation lane metadata or raw source channel identity. This
was adequate for shared worker-control channels but did not enforce the
policy that **durable/full agents have exactly one active Hermes session
owner per concrete agent instance**, regardless of which source channel
or control room the delivery arrives from.

Without session-owner enforcement, two problems arose:

1. A durable agent addressed from channel A and channel B would get
   different Hermes sessions (keyed by `project:X:channel:A` and
   `project:X:channel:B`), losing transcript continuity.
2. Worker pool members sharing a profile config would collapse into the
   same Hermes session if the resolver keyed by profile identity alone.

The `_global/agent-session-boundary-policy` establishes that source
channel/thread/project/task/control-room lanes are source/UI/reply-routing
metadata, **not** transcript ownership.

## Decision

### Session-owner precedence

When the Den Channels adapter converts a delivery to a Hermes
`MessageEvent`, it resolves the session owner using a deterministic
precedence chain:

| Priority | Source field(s) | Owner format | Semantics |
|----------|----------------|--------------|-----------|
| 1 | `session_owner_id` / `sessionOwnerId` | `owner:<value>` | Explicit session owner from Core/Channels/Gateway |
| 2 | `agent_instance_id` / `agentInstanceId` (delivery metadata) | `owner:<value>` | Concrete durable agent instance |
| 3 | `assignment_id` / `targetAssignmentId` (delivery metadata) | `owner:assignment:<id>` | Assignment-scoped session |
| 4 | `worker_run_id` / `workerRunId` (delivery metadata) | `owner:run:<id>` | Run-scoped session |
| 5 | Adapter-level `pool_member_id` | `owner:pool:<value>` | Pool worker slot identity |
| 6 | Adapter-level `agent_instance_id` | `owner:<value>` | Adapter's concrete agent instance |
| 7 | Adapter-level `adapter_instance_id` | `owner:<value>` | Transitional concrete fallback when no pool/agent id |
| 8 | *(none)* | `None` | Fall through to conversation-lane / raw-channel |

> **Note:** The adapter always constructs a fallback `adapter_instance_id` from
> `socket.gethostname():profile:role:gateway` when no explicit config value is
> provided.  This ensures durable agents never silently regress to channel-lane
> sessions during transitional configs that lack cross-service fields.

When a session owner is resolved, it replaces both the raw
`project:<id>:channel:<id>` and conversation-lane `chat_id`.

### Source metadata preservation

All source channel metadata is preserved in the `raw_message` dict:

- `raw_chat_id` — the original `project:<id>:channel:<id>` key
- `channel_id` — numeric Den Channels channel id
- `project_id` — project slug
- `conversation_lane_id` — resolved conversation lane (when session owner
  is absent or `source_lane` scope is explicitly requested)
- `session_owner_id` — resolved session owner key
- `session_scope` — explicit scope request (e.g. `source_lane`)

Reply routing and activity events use the source channel identity from
`raw_message` / `_DeliveryContext`, not the session-owner key.

### source_lane opt-in

Deliveries that explicitly set `session_scope: source_lane` /
`sessionScope: source_lane` in metadata bypass session-owner resolution
and fall back to the #1871 conversation-lane precedence. This is the
**compatibility scope** for UI surfaces that want channel-lane sessions.

### /new/reset scopes

| Scope | Semantics | Who resets |
|-------|-----------|-----------|
| `agent_instance_global` | Default for durable agents. Resets the entire agent instance session. | Durable agent /new |
| `task_series` | Resets task-series context within the agent instance. | Task-bound reset |
| `assignment_run` | Resets only the current assignment run. Worker pool members reset through this lifecycle. | Worker release/cleanup |
| `source_lane` | Resets only the source channel lane context. Explicit opt-in only. | UI channel-lane /new |

## Implementation (den-hermes-only)

Changes are confined to the Den Channels adapter
(`plugins/platforms/den_channels/adapter.py`):

1. **`_resolve_session_owner(delivery, metadata, ...)`**: New pure
   function implementing the session-owner precedence chain. Returns an
   `owner:` prefixed key string or `None`.
2. **`delivery_to_event()`**: Calls `_resolve_session_owner` first. When
   an owner is resolved and `session_scope` is not `source_lane`, uses
   the owner key as `chat_id`. Falls through to `_resolve_conversation_lane`
   when no owner is available or source_lane is requested.
3. **`_DeliveryContext`**: Gains a `session_owner_id` field.
4. **`_build_context()`**: Records the resolved `session_owner_id`.
5. **`raw_message`**: Carries `session_owner_id`, `session_scope`, and
   `conversation_lane_id` for downstream context resolution and reply
   routing.

## Non-goals

- This does not change Den Core, Den Channels, Den Gateway, or Den Host
  behavior. Those cross-service changes should be tracked as separate
  Den tasks.
- Hermes session transcripts remain non-authoritative; Den Core/Channels/
  Gateway are the source of truth.
- Profile identity is **not** used as a session owner to avoid collapsing
  distinct concrete worker instances that share a profile config.

## Verification

- Same concrete durable agent instance from two different source channels
  resolves to the same Hermes session key while retaining different
  `raw_chat_id`/source metadata.
- Two concrete worker instances sharing one profile resolve to different
  session owners/session keys.
- Assignment/run scope is distinct per assignment/run and does not share
  transcript context.
- Explicit `source_lane` / `conversationLaneId` remains an opt-in
  compatibility scope and is labeled as such.
- Activity/reply context still forwards source and target-work metadata.

## Live smoke checklist

Unit tests cover the precedence chain and metadata preservation. Live
verification requires:

1. Service restart/redeploy of the Hermes gateway with the updated
   adapter.
2. Send a delivery to a durable agent from two different channels and
   verify the session key is identical.
3. Verify two pool workers with the same profile get distinct sessions.
4. Verify reply routing still reaches the source channel.
5. Verify `/new` at each scope produces the expected reset behavior.

## References

- #1719: Session scoping — same channel shares session regardless of sender
- #1871: Explicit conversation lane precedence
- #1890: Agent instance global sessions (this task)
- `_global/agent-session-boundary-policy`: Accepted guidance document
- `docs/den-channels-session-scoping-1719.md`: Original session scoping note
- `docs/den-channels-session-lanes-1871.md`: Conversation lane ADR

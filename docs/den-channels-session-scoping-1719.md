# Den Channels session scoping (#1719)

## Intent

Den Channels project/channel lanes are shared planning lanes by design. A
channel lane should maintain one continuous conversation session regardless
of which member sends a message — different senders in the same channel share
a single lane, and `/new` resets the lane's session, not a per-sender session.

## Session key rules

```
Same channel → same session key → same conversation lane
Different channel → different session key → independent conversation lane
Thread/task lane → thread-qualified session key → distinct sub-lane
/new resets only the current lane
```

Den Channels session keys are built from the platform (`den_channels`),
chat type (`channel` or `thread`), and the `project:channel` chat ID.
User/sender identifiers are intentionally excluded so all participants in
the same channel share one session.

## Explicit session lanes (#1871)

The raw channel id model above has been extended with an explicit conversation
lane contract. When delivery metadata includes lane-selection fields
(`conversationLaneId`, `target_task_id`, `assignment_id`, `worker_run_id`),
the adapter uses them to construct a lane-specific session key instead of the
default `project:<id>:channel:<id>` key. This allows a shared worker-control
channel to maintain distinct sessions per target task/assignment/run.

See `docs/den-channels-session-lanes-1871.md` for the full lane-selection
precedence contract and implementation details.

## Implementation

### Adapter level (`plugins/platforms/den_channels/adapter.py`)

- `_resolve_conversation_lane(delivery, metadata)` implements lane-selection
  precedence: explicit lane id > target task > assignment > worker run > channel.
- `delivery_to_event()` uses the resolved lane id as `chat_id` when present.
- `_build_context()` calls `build_session_key(event.source, group_sessions_per_user=False)`
  to compute the adapter's internal session context key — always shared per lane.
- `delivery_to_event()` sets `user_name=sender` (for display prefix) but does
  **not** set `user_id` on the source. Without `user_id`, the GatewayRunner's
  `build_session_key` (which uses its own `GatewayConfig.group_sessions_per_user`
  defaulting to `True`) cannot fork by sender.
- `__init__` sets `config.extra["group_sessions_per_user"] = False` so the
  base adapter's `handle_message()` also builds a shared session key.

### Upstream gateway boundary

The Hermes Gateway's `SessionStore._generate_session_key()` reads
`group_sessions_per_user` from `GatewayConfig` (default `True`). Removing
`user_id` from the Den Channels source prevents sender forking at the
session-store level without changing the global gateway config or profile
settings.

## Verification

- Two messages from different senders in the same channel produce the same
  session key.
- Different channels produce different session keys.
- Thread/task-qualified events produce distinct thread-qualified keys.
- Same channel with different target-task metadata produces distinct session
  lanes (#1871).
- Same explicit lane id across different source channels shares session (#1871).
- Sender identity (user_id) does not fork the session key with or without
  explicit lane metadata (#1871).
- Discord/Telegram platforms preserve their configured `group_sessions_per_user`
  isolation (Den does not touch their source construction).

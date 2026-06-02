# Den Channels Session Lanes (#1871)

## Status

Accepted and implemented in den-hermes only. Cross-service contract changes
(Den Core, Den Channels, Den Gateway API) are staged as follow-up tasks.

## Context

The Den Channels -> Hermes adapter keyed Hermes conversational continuity by
the raw source channel:

```
chat_id = project:<sourceProjectId>:channel:<channelId>
thread_id = thread:<threadRootMessageId>   (when present)
Hermes session key = build_session_key(SessionSource(...))
```

This was adequate when one channel mapped to one project planning lane, but it
breaks for an Operations Hub / shared worker-control model where:

1. One control room carries work for **many** target projects/tasks/assignments.
2. Source/control conversation and target work are explicitly distinct (#1839/#1845).
3. The same target work may intentionally reuse a lane across source surfaces
   (e.g., a coder run started from one channel and continued from another).

Earlier fixes also showed that using `user_id` presence/absence to influence
session identity is too implicit (#1719, #1795): sender identity/auth and
conversational lane identity must be separate concerns.

## Decision

Introduce an explicit Den conversation lane contract so Hermes session
continuity is keyed by a Den-owned lane id rather than accidentally by raw
channel id.

### Lane-selection precedence

When the Den Channels adapter converts a delivery to a Hermes `MessageEvent`,
it resolves a conversation lane id using this deterministic precedence chain:

| Priority | Source field(s) | Lane id format | Semantics |
|----------|----------------|----------------|-----------|
| 1 | `conversationLaneId` or `hermesSessionKey` | `lane:<value>` | Explicit Den-owned lane id from Core/Channels/Gateway |
| 2 | `target_task_id` / `targetTaskId` | `lane:<project>:task:<id>` | Target-task-scoped lane |
| 3 | `assignment_id` / `targetAssignmentId` | `lane:<project>:assignment:<id>` | Assignment-scoped lane |
| 4 | `worker_run_id` / `workerRunId` | `lane:<project>:run:<id>` | Worker-run-scoped lane |
| 5 | *(none of the above)* | `None` | Fall back to `project:<id>:channel:<id>` |

When a lane id is resolved, it replaces the default `project:...:channel:...`
chat_id used in the Hermes `SessionSource`. Thread qualification is preserved
on top of the lane id when `thread_root_message_id` is present.

### Sender identity separation

`user_id` is never set on the Hermes `SessionSource` for Den Channels events,
regardless of lane selection. `user_name` (sender display name) is preserved
for message prefixing. This ensures:

- Same channel + same lane -> same session for all participants.
- Same channel + different lanes (e.g., different target tasks) -> distinct sessions.
- Sender identity is available for auth/display without forking sessions.

### Preserved behaviors

- **#1719**: Same project/channel messages share context; different senders in
  the same channel do not fork by sender.
- **#1795**: Trusted internal Den deliveries queue safely while busy;
  unauthorized external busy-session messages remain blocked.
- `/new` resets only the current lane's session.

### Implementation (den-hermes-only)

Changes are confined to the Den Channels adapter (`plugins/platforms/den_channels/adapter.py`):

1. **`_resolve_conversation_lane(delivery, metadata)`**: New pure function that
   implements the precedence chain. Returns a lane id string or `None`.
2. **`delivery_to_event()`**: Calls `_resolve_conversation_lane` and uses the
   result as `chat_id` when non-None. Preserves `raw_chat_id` and
   `conversation_lane_id` in the event's `raw_message` for downstream context
   resolution and reply routing.
3. **`_DeliveryContext`**: Gains a `conversation_lane_id` field.
4. **`_build_context()`**: Extracts and records `conversation_lane_id`.
5. **`_clear_context()`**: Cleans up lane-keyed context mappings.
6. **`_set_activity_environment()`**: Forwards `conversationLaneId` to the
   activity context for tool-activity breadcrumbs.

### Non-goals

- Hermes session transcripts are not workflow truth; Den Core/Channels/Gateway
  remain authoritative.
- No change to Discord/Telegram group session behavior.
- No requirement for every channel to share one profile-global session.
- No arbitrary channel scrollback fetching to compensate for missing lane context.

## Consequences

### Positive

- A shared worker-control channel can serve multiple target tasks/assignments
  without session collision.
- Direct-agent requests can carry a lane selector independent of sender identity.
- Same target work can intentionally reuse a lane across source surfaces when
  Core/Channels/Gateway provide the same explicit lane id.
- The existing default behavior is unchanged for deliveries without lane metadata.

### Follow-up work needed

This den-hermes implementation is a **consumer-side** contract. Full end-to-end
support requires:

1. **Den Gateway API**: Delivery payloads should accept `conversationLaneId`
   and pass it through to the adapter.
2. **Den Channels**: Direct-agent message wake payloads should carry
   `targetTaskId` / `assignmentId` / `workerRunId` metadata that flows to
   delivery metadata.
3. **Den Core**: Worker task/assignment routing should set lane metadata on
   outbound delivery requests.
4. **Den Channels / Gateway**: `/new` semantics should be scoped to the
   resolved lane, not the raw channel.

These cross-service changes should be tracked as separate Den tasks.

## References

- #1719: Session scoping — same channel shares session regardless of sender
- #1795: Internal events queue while busy, not auth-dropped
- #1839: Source-vs-target attribution fields
- #1845: Operations Hub / shared worker-control model
- `docs/den-channels-session-scoping-1719.md`: Original session scoping note
- `docs/patches/1795-busy-internal-auth.md`: Busy-session auth fix

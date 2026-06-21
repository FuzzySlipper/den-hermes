# Hermes tool-call activity breadcrumbs (#1528)

Den-owned Hermes plugin code emits bounded tool-call breadcrumbs as successor Observation `agent_activity.v1` events during Den Channels delivery processing.

## Ownership boundary

- Capture is Hermes/Den-bridge-specific and lives in the Den Channels Hermes plugin.
- Persistence and non-wake semantics are owned by Den Observation:
  - Observation: `POST /v1/observation/activity-events`
- Legacy Den Channels breadcrumb writes are retired for Hermes:
  - Do **not** write `POST /api/channel-activity-events`.
  - Do **not** write `POST /api/channels/{channelId}/activity-events`.
- Breadcrumb failures are best-effort and must not block final replies.

## Emitted shape

The plugin registers `pre_tool_call` and `post_tool_call` hooks. While a Den Channels delivery is processing, the adapter binds a bounded activity context in a per-task `contextvars.ContextVar` for the hook callbacks, with a process-environment fallback retained only for compatibility/tests. Each Observation event carries:

- request envelope: `source_domain: runtime`, `event_type`, optional `agent_identity`
- payload envelope: `kind: agent_activity.v1`, `schema_version: 1`, `adapter: hermes`, `surface: channel`
- `session_key`, `tool_name`, and optional `work_ref` with project/task/channel/run identifiers
- stable breadcrumb metadata such as `sequence`, `dedupeKey`, display block, parent session, parent identity, worker run, and worker role

## Coalescing and preview safety

Adjacent duplicate tool calls with the same tool name and normalized preview reuse the same sequence/dedupe key and increment count, so UI can render rows such as:

```text
skill_view ×2: {"name": "den-mcp"}
terminal: {"command": "python - <<'PY' ..."}
```

Secret-looking keys/values are redacted and previews are truncated before they leave the plugin.

## Failure behavior

Activity emission is isolated from tool execution and final response handling:

- Hook exceptions are swallowed/logged at debug level.
- Observation failures do not alter final reply terminalization.
- Activity events are not posted as chat messages.

# Hermes tool-call activity breadcrumbs (#1528)

Den-owned Hermes plugin code emits bounded tool-call breadcrumbs as Den Channels activity events during Den Channels delivery processing.

## Ownership boundary

- Capture is Hermes/Den-bridge-specific and lives in the Den Channels Hermes plugin.
- Persistence and non-wake semantics are owned by Den Gateway/Channels:
  - Gateway: `POST /api/channel-activity-events`
  - Channels: `POST /api/channels/{channelId}/activity-events`
- Breadcrumb failures are best-effort and must not block final replies.

## Emitted shape

The plugin registers `pre_tool_call` and `post_tool_call` hooks. While a Den Channels delivery is processing, the adapter binds a bounded activity context in a per-task `contextvars.ContextVar` for the hook callbacks, with a process-environment fallback retained only for compatibility/tests. Each event carries:

- `channelId`, `projectId`, `agentIdentity`
- `deliveryRequestId`, `hermesSessionKey`, optional task/thread/anchor IDs
- `eventType: tool_call_started | tool_call_completed | tool_call_failed`
- `status: started | completed | failed`
- bounded `title`, `summary`, `previewJson`, `metadataJson`
- stable `sequence` and `dedupeKey`

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
- Gateway/Channels failures do not alter final reply terminalization.
- Activity events are not posted as chat messages.

# #1795 — Root Cause: Den Channels Internal Events Dropped as Unauthorized in Busy Sessions

## Summary

Den Channels direct-agent deliveries are built by the Den Channels adapter with
`internal=True` and no `user_id`. The Hermes Gateway cold path (`_handle_message`)
correctly skips auth for internal events. The busy path (`_handle_active_session_busy_message`)
did **not** — it unconditionally called `_is_user_authorized()`, which rejects any event
without a `user_id`. This caused trusted Den Gateway delivery events to be dropped
as unauthorized when the target session was busy.

## Component boundary

| Component | File | Behavior |
|-----------|------|----------|
| Den Channels adapter | `den_hermes/plugins/platforms/den_channels/adapter.py:760` | Builds `MessageEvent(internal=True, user_name=sender)` — correct |
| Gateway cold path | `hermes-agent/gateway/run.py:6970` | `if is_internal: pass` — skips auth, correct |
| Gateway busy path (before fix) | `hermes-agent/gateway/run.py:3156` | `if not self._is_user_authorized(event.source)` — **no internal check, drops trusted events** |
| `_is_user_authorized` | `hermes-agent/gateway/run.py:6617` | `if not user_id: return False` — rejects user_id=None |

## Impact

- **Idle session**: Delivery processed normally (cold path picks it up)
- **Busy session**: Delivery auth-rejected, logged as "Dropping message from unauthorized user in active session", recorded as `wake_event` / `recorded, pending claim/completion` in Den Channels, but never claimed or delivered
- **Recovery**: The next user message or session expiry would eventually trigger a new delivery cycle, but the delivery appeared stalled during the busy period

## Fix

A three-line addition at the top of `_handle_active_session_busy_message`, before the auth gate:

```python
if getattr(event, "internal", False):
    self._queue_or_replace_pending_event(session_key, event)
    return True
```

This matches the existing cold-path behavior: internal events skip user authorization
and are FIFO-queued while the session is busy.

## Security: existing protection preserved

The `#17775` auth gate for non-internal events from unauthorized shared-chat users
remains fully intact — it is checked immediately after the internal-event guard.
The fix only adds a preceding check for trusted internal delivery events.

## Upstream status

- Hermes gateway commit: `8738cb92c3a57c012eaf550b27824717df1af9bf`
- Patch stored as `docs/patches/1795-busy-internal-auth.patch`
- Not pushed upstream; Den-owned retention artifact only

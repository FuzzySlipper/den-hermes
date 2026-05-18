# Den Channels busy delivery queueing (#1509)

## Intent

Den Channels Gateway deliveries are **internal Hermes Gateway events** backed by
Den Gateway delivery-request state. When one arrives while the target Hermes
session is already running, it must not be treated like an ordinary chat follow-up
that can interrupt, steer, or receive a visible busy acknowledgement.

The durable policy is:

- Den Channels internal deliveries always use non-interrupting FIFO queue
  semantics while the lane is busy.
- A queued delivery must not send a channel-visible busy acknowledgement, because
  the adapter's visible send path is also the final `gateway_delivery` completion
  handle for the delivery request.
- Operators should inspect Gateway status diagnostics for pending queue depth
  rather than expecting an in-channel busy ack.
- The running agent should not receive arbitrary user text mid-generation. Any
  future pending-notification signal must appear only at a safe turn/tool
  boundary or as explicit next-turn metadata.

## Current behavior verified

The live Hermes Gateway runtime currently implements the generic internal-event
queueing behavior in `gateway/run.py`:

- `_handle_active_session_busy_message()` computes `effective_mode = "queue"`
  for any `MessageEvent` with `internal=True`, even when
  `display.busy_input_mode` is `interrupt`.
- `_queue_or_replace_pending_event()` sends internal events through
  `_enqueue_fifo()` rather than the normal external-chat merge/replace path.
- Internal busy deliveries return immediately after queueing, so no
  `_send_with_retry()` busy ack is sent.
- Gateway status snapshots include `queued_events` globally and per active
  session (`active_sessions[].queued_events`).

The Den-owned `den_channels` plugin advertises this policy in its adapter binding
capabilities:

```json
{
  "busy_delivery_policy": "force_queue_internal_no_busy_ack",
  "pending_delivery_observability": [
    "gateway_status.active_sessions.queued_events",
    "gateway_status.queued_events"
  ],
  "safe_pending_notifications": "status_only_no_mid_generation_injection"
}
```

## Delivery context attribution

The adapter keeps contexts by immutable delivery id as well as by lane/session.
That is required for same-lane queued deliveries: the latest queued event may
update the lane context, but a final send carrying explicit
`delivery_request_id` metadata must still mark the matching delivery request and
use the matching final dedupe key.

The regression test in `tests/test_den_channels_adapter_queue_context.py` covers
this same-lane case: two deliveries share the same project/channel lane, the
first final send explicitly targets delivery `501`, and the second final send
explicitly targets delivery `502`. Each posts a separate `gateway_delivery` final
message and marks the matching delivery id delivered.

## Normal platform chat messages

Ordinary external chat messages still follow the user's configured Hermes busy
mode:

- `interrupt` (default): attempts to interrupt the running agent and sends an
  interrupt busy ack when enabled.
- `queue`: queues the follow-up for the next turn and sends a queue ack when
  enabled.
- `steer`: tries to inject text after the next tool boundary; if unsupported,
  falls back to queue semantics.

Control-plane commands remain hard controls and are intentionally separate from
Den Channels internal delivery queueing:

- `/stop`, `/new`, and `/reset` remain explicit interrupts/resets.
- `/queue` remains explicit FIFO next-turn behavior.
- `/steer` remains an optional human-chat mid-run steering mechanism.

## Operator checks

For a live `hermes-gateway@<profile>` service, queue observability is available
through the runtime status payload/logging. The relevant fields are:

- `queued_events`: total pending events across sessions/adapters.
- `active_sessions[].queued_events`: queue depth for each active session.
- `active_sessions[].platform`, `chat_id`, and `thread_id`: the Den lane identity.

A queued Den Channels delivery is healthy when it appears in status while the
agent is running, then drains FIFO after the active turn completes and produces a
final `gateway_delivery:{delivery_request_id}:final` reply or a terminal failure.

## Deferred agent-facing signal

No arbitrary in-generation message injection is added here. The safe signal for
now is status-only observability. A future Hermes core enhancement may expose a
structured pending-notification count at a safe tool/turn boundary, but it should
not alter role ordering or insert untrusted user text into an active assistant
message.

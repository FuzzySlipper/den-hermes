# Hermes Bridge ↔ Den Gateway adapter binding and delivery contract

Task: `den-hermes-bridge` #1352  
Status: proposed implementation contract for Gateway/Channels continuation  
Related: `den-gateway` docs `den-gateway-v1-implementation-spec`, `den-gateway-first-pass-follow-ups`; `den-channels` task #1351; Den Core doc `gateway-core-contract`

## 1. Purpose

This document defines the Hermes-specific side of the Den Gateway adapter contract. It lets `den-gateway` continue developing routing, wake delivery, pause/resume, and delivery lifecycle behavior without owning Hermes runtime internals.

The boundary is intentionally thin:

- **Den Gateway owns** routing, wake policy, suppression, retry state, delivery request state, sentinel/outage state, and adapter binding registry.
- **Den Hermes Bridge owns** Hermes profile/runtime transport, local Hermes profile discovery, profile heartbeat registration, delivery polling/claiming, Hermes-specific wake/control message injection, local delivery idempotency, and delivery callbacks back to Gateway.
- **Hermes Agent owns** conversation semantics, tools, profile config, and actual agent execution.

Gateway should treat Hermes as one adapter kind among many. Hermes Bridge should not copy channel membership rules, wake suppression, Den task/review authority, or Gateway sentinel policy.

## 2. Recommended v1 architecture

Run a small local bridge process near the Hermes profile fleet:

```text
Hermes profile(s) / gateway processes
        ^
        | profile-local transport adapter
        v
Den Hermes Bridge adapter loop
        ^                         |
        | heartbeat / claim       | delivery ack/status callback
        v                         v
Den Gateway service
        ^
        | source events, memberships, summaries
        v
Den Core + Den Channels
```

The bridge process has two loops:

1. **Binding heartbeat loop**: discovers configured Hermes profiles/instances and upserts Gateway adapter bindings.
2. **Delivery loop**: claims pending Gateway delivery requests for those bindings, injects the payload into the correct Hermes profile/runtime, and reports lifecycle transitions.

The bridge may be implemented as a Python package/CLI in `den-hermes-bridge` first. It can later grow a long-running daemon or systemd unit. Gateway does not need to know which mechanism the bridge uses internally.

## 3. Adapter kind and binding identity

### 3.1 Adapter kind

Use this stable adapter kind for Hermes profiles:

```text
hermes_profile
```

Use separate adapter kinds only when the transport semantics are meaningfully different, for example:

- `hermes_profile` — a Hermes Agent profile/gateway/CLI instance.
- `hermes_spawned_worker` — optional future split for durable spawned worker processes if they need direct Gateway delivery.
- `hermes_operator_profile` — avoid unless there is a concrete routing need; prefer capabilities metadata.

### 3.2 Adapter instance id

`adapter_instance_id` should identify the live Hermes delivery endpoint, not merely the Den agent identity.

Recommended shape:

```text
hermes:{host}:{profile}:{instance_nonce}
```

Examples:

```text
hermes:den-k8:den-hermes-runner:gateway-main
hermes:den-k8:den-hermes-planner:gateway-main
hermes:den-k8:den-hermes-runner:cli-20260513T154500Z
```

Rules:

- Stable gateway/service processes should use a stable suffix such as `gateway-main`.
- Ephemeral CLI/oneshot instances may use a timestamp or session id suffix.
- Do not put API keys, profile file paths containing secrets, or raw config in the id.
- `agent_identity` remains the Den-facing identity, e.g. `den-hermes-runner`; it is not a substitute for `adapter_instance_id`.

### 3.3 Binding heartbeat payload

Bridge sends heartbeats to Gateway using the existing Gateway shape:

```http
PUT /api/adapter-bindings/heartbeat
Content-Type: application/json
```

Recommended request body:

```json
{
  "adapter_kind": "hermes_profile",
  "adapter_instance_id": "hermes:den-k8:den-hermes-runner:gateway-main",
  "agent_identity": "den-hermes-runner",
  "user_identity": null,
  "project_id": "den-hermes-bridge",
  "role": "runner",
  "status": "active",
  "capabilities_json": {
    "schema_version": 1,
    "delivery_modes": ["notify", "wake", "pause", "resume"],
    "ack_modes": ["bridge_delivered", "bridge_ack", "agent_ack_if_available"],
    "source_kinds": ["channel_message", "task_message", "agent_stream_entry", "notification", "worker_run", "review_round", "review_finding"],
    "supports_control": true,
    "supports_pause_resume": true,
    "supports_wake": true,
    "supports_context_link": true,
    "supports_source_pointer": true
  },
  "metadata_json": {
    "schema_version": 1,
    "host": "den-k8",
    "profile": "den-hermes-runner",
    "profile_home": "/home/agents/profiles/den-hermes-runner",
    "hermes_version": "optional-version-string",
    "bridge_version": "optional-git-sha",
    "transport": "gateway_message|cli_queue|profile_spool|custom",
    "operator_label": "Runner"
  },
  "last_seen_at": "2026-05-13T15:45:00Z",
  "expires_at": "2026-05-13T15:47:00Z"
}
```

Gateway's current table stores `capabilities_json` and `metadata_json` as JSON strings. HTTP implementations may accept either structured objects and serialize them or explicit JSON strings, but the canonical stored value is bounded JSON.

### 3.4 Binding status semantics

- `active`: bridge believes the Hermes profile is reachable and can receive deliveries.
- `degraded`: bridge is alive but delivery transport is partially unavailable, credentials/profile preflight failed, or ack callback is impaired.
- `inactive`: bridge is intentionally withdrawing the binding, profile is stopped, or TTL expiry should remove it from active routing.

Gateway may also expire a stale binding when `expires_at` passes or `last_seen_at` exceeds its TTL policy.

## 4. Delivery request intake and claiming

Gateway owns `delivery_requests`; Bridge only claims and executes them.

### 4.1 Current Gateway v1-compatible flow

The current Gateway spec exposes:

```http
GET  /api/deliveries?status=pending&targetIdentity={identity}&projectId={projectId}&afterId={cursor}&limit={n}
POST /api/deliveries/{id}/mark-delivering
POST /api/deliveries/{id}/ack
POST /api/deliveries/{id}/fail
POST /api/deliveries/{id}/complete
```

Bridge v1 can use this sequence:

1. List pending deliveries for each active binding target.
2. For a selected delivery, call `mark-delivering` with the chosen `adapter_binding_id` / `adapter_instance_id` in the attempt payload if Gateway supports it.
3. If Gateway returns success, inject into Hermes transport.
4. Report `ack`, `complete`, or `fail`.

This is acceptable for early single-bridge deployments.

### 4.2 Recommended Gateway addition: atomic claim

For more than one bridge instance, Gateway should add an atomic claim endpoint so two bridges cannot race:

```http
POST /api/deliveries/claim
Content-Type: application/json
```

Request:

```json
{
  "adapter_kind": "hermes_profile",
  "adapter_instance_id": "hermes:den-k8:den-hermes-runner:gateway-main",
  "project_id": "den-hermes-bridge",
  "agent_identity": "den-hermes-runner",
  "role": "runner",
  "delivery_modes": ["notify", "wake", "pause", "resume"],
  "limit": 10,
  "lease_seconds": 60
}
```

Response:

```json
{
  "status": "claimed",
  "items": [
    {
      "delivery_request_id": 123,
      "attempt_id": 456,
      "lease_expires_at": "2026-05-13T15:46:00Z",
      "delivery": { "...": "full delivery request DTO" }
    }
  ]
}
```

Rules:

- Claim must transition `pending -> delivering` in one Gateway transaction.
- Claim must append a `delivery_attempts` row.
- Expired leases may return to `pending` or become `failed/expired` according to Gateway retry policy.
- Bridge must not claim suppressed, completed, failed, or expired requests.

## 5. Delivery payload consumed by Hermes Bridge

Bridge needs the following Gateway delivery fields. These align with the current `delivery_requests` table plus source-summary/channel contracts.

```json
{
  "id": 123,
  "source_kind": "channel_message",
  "source_id": "789",
  "source_project_id": "den-hermes-bridge",
  "target_type": "agent",
  "target_identity": "den-hermes-runner",
  "project_id": "den-hermes-bridge",
  "task_id": 1352,
  "channel_id": "project-den-hermes-bridge",
  "delivery_mode": "wake",
  "priority": 2,
  "reason": "explicit_mention",
  "context_summary": "Patch asked Runner to switch to task #1352 and define the Hermes bridge side of Gateway delivery.",
  "context_link": "den://project/den-hermes-bridge/task/1352",
  "dedupe_key": "channel-message:789:wake:den-hermes-runner",
  "cascade_depth": 0,
  "expires_at": "2026-05-13T16:00:00Z",
  "metadata": {
    "source_deep_link": "den://channel/project-den-hermes-bridge/message/789",
    "source_sender_identity": "patch",
    "source_sender_type": "user",
    "source_message_kind": "human_text"
  }
}
```

Bridge should treat `context_summary` and `context_link` as the operator-facing wake payload. If deeper context is needed, the awakened Hermes profile should fetch it from Den Core/Channels using the source pointer after it starts, not rely on Gateway embedding full canonical records.

## 6. Hermes delivery envelope

Before injecting into Hermes, Bridge wraps Gateway delivery in a Hermes-visible envelope. This keeps control/wake messages recognizable and dedupable inside Hermes sessions.

### 6.1 Wake/notify envelope

```json
{
  "type": "den_delivery",
  "schema_version": 1,
  "delivery_request_id": 123,
  "attempt_id": 456,
  "delivery_mode": "wake",
  "dedupe_key": "channel-message:789:wake:den-hermes-runner",
  "target": {
    "agent_identity": "den-hermes-runner",
    "project_id": "den-hermes-bridge",
    "role": "runner",
    "adapter_instance_id": "hermes:den-k8:den-hermes-runner:gateway-main"
  },
  "source": {
    "source_kind": "channel_message",
    "source_id": "789",
    "source_project_id": "den-hermes-bridge",
    "channel_id": "project-den-hermes-bridge",
    "task_id": 1352,
    "context_link": "den://project/den-hermes-bridge/task/1352"
  },
  "message": {
    "summary": "Patch asked Runner to switch to task #1352 and define the Hermes bridge side of Gateway delivery.",
    "reason": "explicit_mention",
    "priority": 2
  },
  "instructions": [
    "Refresh Den state before acting.",
    "Use source pointers for full context; do not treat this envelope as canonical task state.",
    "Acknowledge the delivery if the profile can do so."
  ],
  "issued_at": "2026-05-13T15:45:00Z",
  "expires_at": "2026-05-13T16:00:00Z"
}
```

A human-readable rendering may be sent to the profile instead of raw JSON, but it must include `delivery_request_id`, `dedupe_key`, `delivery_mode`, `context_summary`, and `context_link`.

### 6.2 Pause/resume control envelope

Pause/resume deliveries use `delivery_mode` `pause` or `resume` and a control payload type:

```json
{
  "type": "den_control",
  "schema_version": 1,
  "control_type": "pause",
  "delivery_request_id": 124,
  "attempt_id": 457,
  "dedupe_key": "sentinel:outage-20260513:pause:den-hermes-runner",
  "outage_id": "outage-20260513",
  "maintenance_id": "optional-maintenance-id",
  "reason": "planned_den_maintenance",
  "scope": {
    "project_id": null,
    "agent_identity": "den-hermes-runner",
    "role": null
  },
  "instructions": [
    "Do not start new Den-dependent work.",
    "Do not infer task state from stale local context.",
    "Do not retry Den tools in loops while pause is active.",
    "Preserve local state safely and wait for resume/all-clear."
  ],
  "required_ack": true,
  "issued_at": "2026-05-13T15:45:00Z",
  "expires_at": "2026-05-13T16:00:00Z"
}
```

Resume payload:

```json
{
  "type": "den_control",
  "schema_version": 1,
  "control_type": "resume",
  "delivery_request_id": 125,
  "attempt_id": 458,
  "dedupe_key": "sentinel:outage-20260513:resume:den-hermes-runner",
  "outage_id": "outage-20260513",
  "reason": "den_recovered",
  "instructions": [
    "Refresh Den state before continuing.",
    "Re-check task assignment, dependencies, status, review state, and latest messages.",
    "If Den is still unreachable from this profile, remain paused and report degraded status when possible.",
    "If local state conflicts with Den, stop and ask for operator direction."
  ],
  "required_ack": true,
  "issued_at": "2026-05-13T15:55:00Z",
  "expires_at": "2026-05-13T16:10:00Z"
}
```

## 7. Hermes transport options

The contract does not require Gateway to choose or know the Hermes transport. Bridge selects an implementation via profile metadata/config.

Recommended transport interface:

```python
class HermesDeliveryTransport:
    def deliver(self, profile: str, envelope: dict) -> DeliveryTransportResult: ...
```

`DeliveryTransportResult` should include:

- `accepted: bool`
- `transport: str`
- `external_message_id: str | None`
- `session_id: str | None`
- `error_code: str | None`
- `error_message: str | None`

Candidate v1 transports and tradeoffs:

| Transport | Pros | Cons | Recommended use |
| --- | --- | --- | --- |
| Gateway/platform message to the profile's home/operator channel | Uses existing Hermes gateway behavior; visible to human; easy to debug | May be noisy; platform-specific; needs anti-echo care | Good first live path for human-visible wake/control |
| Profile-local spool file/inbox watched by bridge/gateway plugin | Durable, local, testable, platform-neutral | Requires Hermes-side plugin or polling command to consume | Good medium-term path |
| Hermes CLI oneshot/session queue | Simple to prototype; isolated | May spawn extra process rather than wake existing gateway profile | Useful for tests and non-interactive profiles |
| Direct in-process Hermes API | Lowest latency if available | Couples bridge to Hermes internals; version-sensitive | Avoid for v1 unless Hermes exposes stable API |

The default v1 should be explicit and boring: platform/gateway message delivery for running profile gateways, and CLI/spool only for controlled tests.

## 8. Delivery callback contract

Gateway status remains authoritative. Bridge reports lifecycle via Gateway endpoints.

### 8.1 Mark delivering / claim

Bridge calls claim or `mark-delivering` before attempting Hermes transport. This means Gateway can see stuck attempts even if Hermes delivery fails.

Attempt metadata should include:

```json
{
  "adapter_kind": "hermes_profile",
  "adapter_instance_id": "hermes:den-k8:den-hermes-runner:gateway-main",
  "bridge_host": "den-k8",
  "bridge_pid": 12345,
  "transport": "gateway_message",
  "delivery_schema_version": 1
}
```

### 8.2 Delivered

Bridge marks delivered when the Hermes transport accepts the envelope, e.g. message posted, spool write fsynced, or CLI queue accepted.

```http
POST /api/deliveries/{id}/ack
```

Suggested body:

```json
{
  "status": "delivered",
  "adapter_kind": "hermes_profile",
  "adapter_instance_id": "hermes:den-k8:den-hermes-runner:gateway-main",
  "attempt_id": 456,
  "ack_kind": "bridge_delivered",
  "external_message_id": "platform-message-or-spool-id",
  "observed_at": "2026-05-13T15:45:03Z"
}
```

If Gateway wants to distinguish delivered vs acknowledged with separate endpoints, keep `ack_kind=bridge_delivered` and let the endpoint transition `delivering -> delivered`.

### 8.3 Acknowledged

Bridge marks acknowledged when the Hermes profile/agent explicitly acknowledges the delivery, or when a control delivery is durably applied by the bridge on behalf of the profile.

Suggested body:

```json
{
  "status": "acknowledged",
  "attempt_id": 456,
  "ack_kind": "agent_ack_if_available",
  "agent_identity": "den-hermes-runner",
  "session_id": "optional-hermes-session-id",
  "observed_at": "2026-05-13T15:45:08Z"
}
```

Rules:

- `pause` can be acknowledged by the bridge once it has persisted a local paused flag for the target profile and injected/queued the control message.
- `resume` should only be acknowledged if the bridge/profile can verify Den or Gateway is reachable enough to continue; otherwise report `degraded` or fail with `den_still_unreachable`.
- Wake/notify may remain only `delivered` if Hermes has no explicit ack path yet. The task text says agents must ack **if possible**; lack of explicit ack is not a reason to put Hermes internals in Gateway.

### 8.4 Completed

Bridge marks completed when no more adapter work is required for the delivery.

For delivery modes:

- `notify`: complete after delivered/acknowledged.
- `wake`: complete after Hermes profile accepted the wake. Do not wait for the entire agent task to finish; Den task/worker completion is separate canonical state.
- `pause`: complete after paused state is persisted/applied and acked if possible.
- `resume`: complete after resume is delivered and the profile has been instructed to refresh Den state.

```http
POST /api/deliveries/{id}/complete
```

Suggested body:

```json
{
  "attempt_id": 456,
  "completion_kind": "adapter_delivery_complete",
  "observed_at": "2026-05-13T15:45:10Z",
  "metadata": {
    "transport": "gateway_message",
    "external_message_id": "..."
  }
}
```

### 8.5 Failed

Bridge reports failed for adapter/runtime failures, not policy suppression. Gateway owns suppression.

```http
POST /api/deliveries/{id}/fail
```

Suggested body:

```json
{
  "attempt_id": 456,
  "error_code": "hermes_profile_unreachable",
  "error_message": "Profile den-hermes-runner gateway process is not reachable.",
  "retryable": true,
  "observed_at": "2026-05-13T15:45:10Z"
}
```

Recommended error codes:

- `hermes_profile_unreachable`
- `hermes_profile_not_configured`
- `hermes_transport_unavailable`
- `hermes_transport_rejected`
- `bridge_paused_locally`
- `den_still_unreachable`
- `delivery_expired_before_attempt`
- `malformed_delivery_payload`
- `unsupported_delivery_mode`
- `duplicate_delivery_ignored`

## 9. Dedupe and idempotency

Gateway owns global delivery dedupe via `delivery_requests.dedupe_key`. Bridge owns local repeated-delivery safety.

Bridge should maintain a small local idempotency store keyed by:

```text
(adapter_instance_id, delivery_request_id, dedupe_key, delivery_mode)
```

Minimum local states:

- `seen`
- `delivered`
- `acknowledged`
- `completed`
- `failed`
- `ignored_duplicate`

Rules:

- Duplicate `pause` with same `dedupe_key` is safe: re-ack current paused state and do not re-spam the profile unless operator visibility requires it.
- Duplicate `resume` with same `dedupe_key` is safe only if the matching pause/outage id is already resolved locally; otherwise deliver but include cautious Den-refresh instructions.
- Duplicate `wake` should not start multiple Hermes turns. If the same delivery was already delivered/completed, report idempotent success to Gateway if the endpoint supports it; otherwise do nothing except local log.
- If Gateway retries with a new attempt id but same delivery id/dedupe key after prior failure, Bridge may retry only if local state is not `completed`.
- Local idempotency records may expire after Gateway's delivery retention window plus a safety margin.

## 10. Pause/resume behavior inside Hermes profiles

The Hermes profile should treat `den_control` as higher priority than normal wake/notify.

Pause behavior:

1. Ack if possible.
2. Stop starting new Den-dependent work.
3. Avoid retry loops against Den tools.
4. Preserve local work state and logs.
5. Surface a concise paused status to the operator if the profile has a safe path.
6. Wait for resume/all-clear.

Resume behavior:

1. Ack only if Gateway/Den is reachable enough to trust the all-clear.
2. Refresh Den task/messages/guidance before continuing.
3. Re-check assignment/status/dependencies/review state.
4. Continue only if Den still says the work is valid.
5. If local state conflicts with Den, stop and ask Patch/Planner.

Bridge should make this behavior visible in delivered text, but Hermes Agent should ultimately enforce it through loaded guidance/skills/system prompt once a plugin or profile-level instruction mechanism exists.

## 11. Security and privacy

- Never include API keys, profile `.env` contents, OAuth tokens, raw `auth.json`, or full process environment in Gateway bindings or delivery callbacks.
- Profile names, provider/model names, hostnames, process ids, session ids, and log/artifact paths are allowed.
- Gateway delivery payloads should contain compact summaries and source pointers, not full task/review/channel history.
- Bridge should bound metadata sizes before heartbeat/callback.
- Any profile-local spool/inbox should be mode `0600` or live under profile directories with existing profile permissions.
- Service-to-service calls should support the Gateway/Core static token pattern when deployed beyond loopback/trusted LAN.

## 12. Contract gaps / tasks Gateway can continue with

Gateway can proceed immediately with its current endpoints for a single bridge instance, using `GET pending -> mark-delivering -> ack/fail/complete`.

Recommended follow-up additions for robust multi-adapter operation:

1. **Atomic delivery claim endpoint**: `POST /api/deliveries/claim` with adapter binding filters and a lease.
2. **Structured ack body**: allow `ack_kind`, `attempt_id`, `external_message_id`, `session_id`, and `observed_at` so delivered vs agent-ack can be distinguished.
3. **Attempt payload metadata**: accept adapter/transport metadata on `mark-delivering` or claim.
4. **Idempotent terminal callbacks**: duplicate `ack/complete/fail` for the same attempt should return the existing state rather than generic failure where possible.
5. **Delivery DTO includes metadata**: include bounded source metadata from Channels/Core in the delivery response, but preserve source pointers as the authority.

These are Gateway API improvements, not reasons to put Hermes-specific logic into Gateway.

## 13. Suggested Hermes Bridge implementation slices

If #1352 turns into implementation work, split it into tested slices:

1. **Binding model/resolver**: map local Hermes profile config to Gateway heartbeat DTOs.
2. **Gateway client**: typed Python client for heartbeat, list/claim deliveries, ack/fail/complete.
3. **Delivery envelope builder**: converts Gateway delivery DTO to `den_delivery` / `den_control` envelopes.
4. **Local idempotency store**: SQLite or JSONL store for delivery ids/dedupe keys.
5. **Transport adapter interface**: fake transport first, then platform/gateway-message transport.
6. **Pause/resume local state**: profile-scoped paused flags and cautious resume checks.
7. **Systemd/CLI runner**: `den-hermes gateway-bridge run --gateway-url ... --profile ...` later if desired.

Follow strict TDD for code-bearing slices: fake Gateway and fake Hermes transport first, then live smoke against deployed Gateway.

## 14. Open decisions

1. Which Hermes transport should be first live path: platform/gateway message, profile-local spool, or CLI queue?
2. Should Hermes Agent grow a first-class profile inbox/control plugin, or should Bridge remain purely external for v1?
3. Should Gateway expose delivered and acknowledged as separate endpoints, or keep one `ack` endpoint with `ack_kind/status`?
4. How long should bridge-local dedupe records be retained relative to Gateway delivery retention?
5. Should `hermes_spawned_worker` be a separate adapter kind, or should spawned workers stay in Den worker-run state only until a concrete Gateway use case exists?

## 15. Recommended answer for v1

Use the smallest contract that unblocks Gateway while preserving boundaries:

- Bridge registers Hermes profiles as `hermes_profile` adapter bindings.
- Bridge claims/listens for Gateway `delivery_requests` targeted at those bindings.
- Bridge translates requests into `den_delivery` or `den_control` envelopes.
- Bridge injects envelopes via a configurable Hermes transport.
- Bridge reports delivered/acknowledged/completed/failed back to Gateway.
- Bridge dedupes locally and treats pause/resume as idempotent control.
- Gateway remains the only owner of routing, suppression, retries, and delivery truth.

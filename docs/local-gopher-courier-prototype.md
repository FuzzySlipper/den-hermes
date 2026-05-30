# Local Gopher/Courier Prototype

## Overview

This prototype implements a **bounded, deterministic gopher/courier agent** that
babysits delivery events flowing through the Den Gateway → Channels wake bridge.
The gopher does **not** own, orchestrate, or mutate task state. It observes
delivery evidence, validates LLM-proposed actions against a strict schema, and
produces structured evidence packets that a parent orchestrator can consume.

### Architecture / Boundaries

```
Gateway wake/callback events
       |
       v
   [gopher tick]             <-- deterministic FSM + schema validation
       |
       +-- DeliveryEvidence (typed input)
       +-- LLM proposal      (optional — validated against schema)
       +-- FSM selects action <-- fail-closed on invalid input
       |
       v
   EvidencePacket             <-- structured output for orchestrator
```

The gopher's FSM is **always authoritative** over the LLM. The LLM may propose
an action, but the FSM can override it based on:
- Delivery state (already persisted? stuck? fresh unclaimed?)
- Budgets (nudge count, notification count)
- Dedupe suppression (same action on same delivery >2x in 5min)
- Safety constraints (no self-wake, no channel/target mismatch)

## Action Schema

| Action              | Escalation | Description                                   |
|---------------------|-----------|-----------------------------------------------|
| `no_op`             | 0         | Explicitly do nothing                         |
| `wait`              | 1         | Skip tick, check again after `next_check_seconds` |
| `record_observation`| 2         | Log evidence without posting any message      |
| `ack_sender`        | 3         | Acknowledge receipt of a delivery wake        |
| `nudge_target`      | 4         | Send gentle reminder to target agent          |
| `notify_human`      | 5         | Escalate to human operator                    |

### Reasons

- `recorded`, `unclaimed`, `claimed_no_activity`, `provider_slow`
- `tool_waiting`, `suppressed`, `target_offline`, `unknown`, `callback_persisted`

### Thresholds

| Parameter              | Default | Description                                 |
|------------------------|---------|---------------------------------------------|
| `MAX_MESSAGE_LENGTH`   | 2000    | Max chars for nudge/notification message    |
| `MAX_NUDGE_COUNT`      | 3       | Max times to nudge before escalating        |
| `MAX_NOTIFICATION_COUNT` | 2     | Max times to notify human before suppressing |
| `MIN_NEXT_CHECK_SECONDS` | 5     | Minimum time before next check              |
| `MAX_NEXT_CHECK_SECONDS` | 3600   | Maximum time before next check              |
| Dedupe window          | 300s    | Same action on same delivery >2x = suppressed |

### Self-Recursive Wake Guard

The gopher **must never** target itself. Any `target_agent` starting with
`gopher` or `courier` (case-insensitive) is rejected and fail-closed.

## Deduplication

Dedupe keys are derived from `delivery_id` (`d:{delivery_id}`). When the same
action is chosen for the same delivery more than twice within 5 minutes,
subsequent identical actions are suppressed to `no_op` with reason `suppressed`.
Different actions for the same delivery are tracked independently.

## Failure Behavior

| Condition                          | FSM Action              | Reason                |
|------------------------------------|-------------------------|-----------------------|
| Invalid model output (schema fail) | `record_observation`    | `unknown`             |
| Invalid + stuck evidence           | `notify_human`          | `target_offline`      |
| No model output (None)             | `record_observation`    | `unknown`             |
| No model + already persisted       | `no_op`                 | `callback_persisted`  |
| Hallucinated action/reason         | `record_observation`    | `unknown`             |
| Self-target (gopher/courier)       | `record_observation`    | `unknown`             |
| Target/channel mismatch            | `record_observation`    | `unknown`             |
| Message too long                   | `record_observation`    | `unknown`             |

## What the Prototype Does NOT Have

- **No task ownership** — does not claim, assign, or transition task state
- **No orchestration** — does not start, kill, quarantine, pause, or resume
- **No production posting** — no live Channels/Gateway mutation routes
- **No credentials/secrets** — model endpoint URL and model name are
  configurable, never hard-coded
- **No persistent state** — dedupe records and counts are scoped to the
  process lifetime

## Files

| File | Purpose |
|------|---------|
| `den_hermes/gopher.py` | Core gopher module: data models, validation, FSM, dedupe, evidence packet builder |
| `tests/test_gopher.py` | Comprehensive tests covering all acceptance scenarios |
| `scripts/evaluate_gopher_model.py` | Offline/live model evaluation against action schema |
| `docs/local-gopher-courier-prototype.md` | This runbook |

## Testing

### Run focused gopher tests

```bash
cd /home/dev/den-hermes
python -m pytest tests/test_gopher.py -v
```

### Run with coverage

```bash
python -m pytest tests/test_gopher.py -v --cov=den_hermes.gopher
```

### Run all existing tests (non-gopher) for regression check

```bash
python -m pytest tests/ --timeout=30 -x -q
```

## Running the Model Eval Script

### Offline/fake mode (no model needed)

```bash
cd /home/dev/den-hermes
python scripts/evaluate_gopher_model.py --offline
```

Expected output: 5/5 fixtures valid, action matches.

### Live mode (local LLM)

```bash
# OpenAI-compatible endpoint (den-nimo Lemonade / vLLM / etc.)
python scripts/evaluate_gopher_model.py \
    --base-url http://192.168.1.23:13305/v1 \
    --model qwen3.6-35b-a3b-gguf

# Ollama endpoint
python scripts/evaluate_gopher_model.py \
    --base-url http://192.168.1.23:13305/api/chat \
    --model gemma-4-26b-a4b-it-gguf \
    --endpoint-type ollama
```

Environment variables: `GOPHER_EVAL_BASE_URL`, `GOPHER_EVAL_MODEL`.

Output: per-fixture schema validity, action match, latency, and a summary
table. Results can be written to a JSON file with `--output <path>`.

### Recommended den-nimo config for testing

The endpoint at `192.168.1.23:13305` serves **den-nimo Lemonade** API.
Recommended local LLM candidates (from operator notes):

- **Qwen3.6-35B-A3B-GGUF** — good schema adherence, fast
- **Gemma-4-26B-A4B-it-GGUF** — also strong, slightly different tokenizer
- **Avoid GLM-4.7** unless it passes schema eval independently

The eval script does **no posting** — it only sends prompts, validates
responses, and reports. No live Channels/Gateway mutations occur.

## Current #1744 Gateway Evidence

From the completed #1744 delivery waterfall:

- Wake channel message: **#1359**
- Gateway delivery request: **#657**
- Final reply: **#1360**
- Status: **callback_persisted**
- Gateway span: **589.4ms**
- Bridge span: **3099.2ms**
- Provider timing: **unavailable** (carried as `provider_timing_unavailable` label)

These values are used in test fixtures and the offline eval fixtures as
reference evidence shapes.

## Programmatic API

```python
from den_hermes import GopherAction, run_gopher_tick
from den_hermes.gopher import DeliveryEvidence, EvidencePacket

evidence = DeliveryEvidence(
    message_id="msg-1359",
    delivery_id="gw-del-657",
    target_agent="worker-alpha",
    channel_id="wake-general",
    status="unclaimed",
    gateway_span_ms=589.4,
    bridge_span_ms=3099.2,
    provider_timing_unavailable=False,
)

model_json = {
    "action": "ack_sender",
    "reason": "recorded",
    "target_agent": "worker-alpha",
    "channel_id": "wake-general",
    "message": "Delivery received. Observing.",
    "next_check_seconds": 30,
}

dedupe_records: dict[str, IncidentDedupeRecord] = {}
packet = run_gopher_tick(
    evidence=evidence,
    model_raw_json=model_json,
    dedupe_records=dedupe_records,
    nudge_count=0,
    notify_count=0,
)

print(packet.fsm_action)    # GopherAction.ACK_SENDER
print(packet.schema_valid)  # True
print(packet.dedupe_suppressed)  # False
```

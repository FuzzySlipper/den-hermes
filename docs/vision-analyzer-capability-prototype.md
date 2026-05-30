# Vision Analyzer Capability — Prototype

**Capability id:** `vision.analyze_image.v1`
**Owner:** `den-hermes-bridge`
**Side effect level:** `read_only`
**Version:** `1.0.0`

## Architecture / Boundaries

```
Core (den-core)         den-hermes-bridge (this repo)
┌─────────────┐         ┌─────────────────────────────────┐
│ Capability   │         │ External Executor HTTP endpoint │
│ Registry     │ ──────> │ POST /vision/analyze-image      │
│ V1 Substrate │         │                                 │
│ (task #1749) │         │ Vision Analyzer Module           │
└─────────────┘         │ - Validation                    │
                         │ - Prompt builder                │
                         │ - Fake analyzer                 │
                         │ - Output parser                 │
                         │ - Capability definition          │
                         └─────────────────────────────────┘
```

The vision analyzer lives **entirely in `den-hermes`** — it is an external executor
prototype. No implementation lives in Core. Core provides the capability
registration/invocation substrate; this module provides the executor that Core
would call via HTTP.

## Core Registration Shape

The `build_capability_definition()` function in `den_hermes/vision_analyzer.py`
returns a JSON-serializable dict suitable for Core registration. Key fields:

| Field | Value |
|-------|-------|
| `capability_id` | `vision.analyze_image.v1` |
| `side_effect_level` | `read_only` |
| `execution_type` | `http_endpoint` |
| `timeout_seconds` | 60 |
| `max_request_bytes` | 10 MB |
| `security.read_only` | `true` |
| `security.visible_writes_allowed` | `false` |
| `security.allow_data_urls` | `false` |

The definition includes request/response JSON schemas, default and fallback
model configs using tags/placeholders, and **no secrets**.

## Executor Endpoint

### `POST /vision/analyze-image`

Accepts the external executor request envelope JSON.

**Request envelope fields:**

| Field | Type | Required |
|-------|------|----------|
| `invocation_id` | string | yes |
| `capability_id` | string | yes |
| `capability_version` | string | yes |
| `caller` | string | yes |
| `side_effect_level` | string | yes (must be `read_only`) |
| `deadline_utc` | number (float) or ISO-8601 string | yes |
| `request` | object | yes |
| `safety` | object | yes |

**Core wire format note:** The Core runtime uses `System.Text.Json.JsonSerializerDefaults.Web`, which serializes
field names as **camelCase** (e.g. `invocationId`, `capabilityId`, `sideEffectLevel`, `deadlineUtc`).
The HTTP handler accepts both camelCase (Core) and snake_case (prototype) field names interchangeably.
The `deadlineUtc` / `deadline_utc` field accepts both an ISO-8601 string (e.g. `"2026-05-29T22:00:00Z"`)
and a numeric epoch-seconds value for backward compatibility.

**Request fields** (inside `request`):

| Field | Type | Required | Default |
|-------|------|----------|---------|
| `image_ref` | string (URL or resource ref) | yes | — |
| `mode` | enum (see below) | no | `general` |
| `question` | string | yes | — |
| `include_ocr` | boolean | no | `false` |
| `include_regions` | boolean | no | `false` |
| `output_detail` | `auto`/`low`/`high` | no | `auto` |
| `locale_hint` | string | no | `""` |
| `ui_context` | string | no | `""` |

**Allowed modes:** `general`, `ui_screenshot`, `diagram`, `ocr`, `error_screen`, `diff`

### `GET /health`

Returns `{"status": "ok", "capability_id": "vision.analyze_image.v1"}`.

## Request / Response Schema

### Request Schema (JSON)

```json
{
  "type": "object",
  "required": ["image_ref", "question"],
  "properties": {
    "image_ref": {"type": "string"},
    "mode": {"type": "string", "enum": ["general", "ui_screenshot", "diagram", "ocr", "error_screen", "diff"]},
    "question": {"type": "string"},
    "include_ocr": {"type": "boolean"},
    "include_regions": {"type": "boolean"},
    "output_detail": {"type": "string", "enum": ["auto", "low", "high"]},
    "locale_hint": {"type": "string"},
    "ui_context": {"type": "string"}
  }
}
```

### Response Envelope (Core-compatible wire format)

On the wire, `output` is a JSON-encoded string because Core stores `invocation.OutputJson`
from this field. The contained JSON string has the Vision Analyzer structured output schema.

```json
{
  "status": "completed",
  "output_summary": "Brief summary of the analysis",
  "output": "{\"summary\":\"string\",\"answer\":\"string\",\"observations\":[...],\"confidence\":0.95}",
  "output_artifact_refs": [],
  "model": {
    "provider": "local-fake",
    "name": "fake-analyzer-local-v1",
    "version": "1.0.0"
  },
  "timings_ms": {"total_ms": 1},
  "cost": {"total_cost": 0.0},
  "metadata": {
    "capability_id": "vision.analyze_image.v1",
    "capability_version": "1.0.0",
    "mode": "ui_screenshot",
    "include_ocr": false,
    "currency": "USD"
  }
}
```

**Difference from internal Python helpers:** The Python `execute_vision_analysis()` returns
a `ResponseEnvelope` whose `output` field is always a JSON string. Use `extract_output_json(resp)`
to get the structured `VisionOutput` dict back. Internal helpers (`VisionOutput`, `run_fake_analyzer`,
`parse_model_output`) work with structured Python objects; only the envelope serialization
to Core uses the JSON string format.

The `model` field is always a JSON object with `provider`, `name`, and `version` — never
a bare string. `timings_ms` values are integers. `cost` values are numeric only; any
currency indicator moves to `metadata.currency`.

**Status values:** `completed`, `invalid_request`, `safety_rejected`,
`model_error`, `timeout`, `internal_error`

## Safety Rules

1. **`side_effect_level` must be `read_only`** — the executor rejects any
   request with a different value.

2. **`safety.visible_writes_allowed` must be `false`** — this is enforced at
   the validation layer.

3. **No raw data: URLs** — `data:` URLs are rejected. Use HTTPS URLs or
   resource references.

4. **Image ref length limit** — max 8192 bytes (UTF-8 encoded).

5. **Image/OCR text is untrusted data** — the prompt builder explicitly
   instructs the model to treat all text visible in the image as untrusted.
   Prompt-injection-looking text must be reported in the
   `injection_like_text` field and **never obeyed**.

6. **No secrets in code** — model endpoint URLs are generic placeholders.
   API keys/tokens must be configured via environment variables only.

7. **Deadline validation** — requests with expired or impossibly-far-future
   deadlines are rejected.

## Benchmark Commands

### Offline/fake mode (no model calls):

```bash
python scripts/evaluate_vision_analyzer.py --offline \
    --output /tmp/vision-analyzer-eval.json
```

### Against live endpoint (OpenAI-compatible):

```bash
python scripts/evaluate_vision_analyzer.py \
    --base-url http://den-nimo:13305/v1 \
    --model qwen3.6-35b-a3b-gguf \
    --output /tmp/vision-analyzer-live-eval.json
```

### Environment variables:

| Variable | Overrides |
|----------|-----------|
| `VISION_EVAL_BASE_URL` | `--base-url` |
| `VISION_EVAL_MODEL` | `--model` |
| `VISION_ANALYZER_HOST` | `--host` |
| `VISION_ANALYZER_PORT` | `--port` |

## Running Tests

```bash
python -m pytest tests/test_vision_analyzer.py -v
```

### Coverage

- 67+ tests covering validation, execution, parsing, prompt building,
  injection detection, capability definition, all modes, error shaping.
- HTTP handler tested manually (health, happy POST, error POST).
- Offline benchmark verifies all 6 fixtures pass rubric + schema.

## HTTP Service

```bash
# Start service (fake analyzer mode — safe default):
python scripts/serve_vision_analyzer.py

# Start on specific host/port:
python scripts/serve_vision_analyzer.py --host 0.0.0.0 --port 8080

# Enable real model path (requires external config):
python scripts/serve_vision_analyzer.py --real-model
```

The HTTP service uses stdlib `http.server` — no external dependencies.

## Local / Cloud Model Configuration (no secrets)

### Recommended Default / Fallback Guidance

| Rank | Provider | Endpoint | When to use |
|------|----------|----------|-------------|
| 1 | **Local fake analyzer** | (none) | Default, testing, offline benchmark |
| 2 | **den-nimo Lemonade** | `http://den-nimo:13305/v1` | When den-nimo has a multimodal vision model available |
| 3 | **den-nimo (alternate)** | `http://192.168.1.23:13305/v1` | Fallback LAN endpoint |
| 4 | **vLLM** | `http://192.168.1.23:8000/v1` | When vLLM GPU node is online with vision support |

**Current status:** No live vision model is confirmed available on den-nimo
or vLLM at this time. The offline/fake harness passes and records explicit
`reason_local_only`. When a model becomes available, configure via:

```bash
export VISION_EVAL_BASE_URL=http://den-nimo:13305/v1
export VISION_EVAL_MODEL=<model-name>
```

### Cloud Model Guidance

If cloud models (OpenAI GPT-4V, Claude 3 Vision) are preferred, configure via
environment variables only. **Never commit API keys or tokens.**

## Module Structure

```
den_hermes/vision_analyzer.py
├── Constants (CAPABILITY_ID, versions, limits)
├── Enums (AnalysisMode, ResponseStatus, SideEffectLevel)
├── Dataclasses
│   ├── SafetySpec
│   ├── ExecutorRequest
│   ├── VisionRequest
│   ├── VisionOutput
│   ├── ResponseEnvelope
│   ├── ModelCandidate
│   ├── BenchmarkFixture
│   └── BenchmarkResult
├── Validation
│   ├── validate_executor_request()
│   ├── validate_vision_request()
│   └── validate_safety_for_request()
├── Prompt Builder
│   ├── build_vision_prompt()
│   ├── _build_system_prompt()
│   └── _build_user_prompt()
├── Fake Analyzer
│   └── run_fake_analyzer()
├── Parser / Normalizer
│   ├── parse_model_output()
│   └── _validate_output_schema()
├── Executor
│   └── execute_vision_analysis()
└── Capability Definition
    └── build_capability_definition()

scripts/serve_vision_analyzer.py       — HTTP service
scripts/evaluate_vision_analyzer.py    — Benchmark/eval harness
tests/test_vision_analyzer.py          — Test suite
```

## Known Gaps

- **Real model path:** The `execute_vision_analysis()` function's live model
  path returns a `MODEL_ERROR` — it requires OpenAI-compatible endpoint
  integration. The fake analyzer is the only working execution path.
- **No streaming:** All responses are synchronous. Streaming vision output
  is not implemented.
- **No caching:** Each invocation runs fresh analysis with no result cache.
- **No image validation beyond ref checks:** Image content (e.g., corrupt
  images, unsupported formats) is not validated server-side — delegated to
  the model endpoint.
- **Coordinate system:** `region_coordinate_space` is a placeholder string
  in the fake analyzer; real implementation would need coordinate parsing.
- **No rate limiting:** The HTTP service has no built-in rate limiter.
- **No authentication:** The HTTP service has no auth layer — depends on
  network-level access control (internal LAN).
- **Live model availability:** No local vision model confirmed available on
  den-nimo or vLLM at prototype time.

#!/usr/bin/env python3
"""Offline/fake model evaluation script for the gopher courier prototype.

This script evaluates an LLM's ability to produce valid gopher action JSON
against the schema defined in den_hermes.gopher. It sends compact fixtures
to an OpenAI-compatible endpoint and reports:

- Action-schema validity rate
- Latency (mean, min, max)
- Distribution of chosen actions
- Invalid reasons breakdown
- Per-fixture details

Usage:
    # Offline/fake mode (no model calls):
    python scripts/evaluate_gopher_model.py --offline

    # Against local LLM (den-nimo Lemonade / Ollama):
    python scripts/evaluate_gopher_model.py \\
        --base-url http://192.168.1.23:13305/v1 \\
        --model qwen3.6-35b-a3b-gguf

    # Against Ollama chat endpoint:
    python scripts/evaluate_gopher_model.py \\
        --base-url http://192.168.1.23:13305/api/chat \\
        --model gemma-4-26b-a4b-it-gguf \\
        --endpoint-type ollama

Environment variables (overrides CLI):
    GOPHER_EVAL_BASE_URL
    GOPHER_EVAL_MODEL
"""

from __future__ import annotations

import json
import os
import sys
import time
import argparse
from dataclasses import dataclass, field
from typing import Any
from pathlib import Path


# ---------------------------------------------------------------------------
# Add repo root to path
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Imports from module under test
# ---------------------------------------------------------------------------

from den_hermes.gopher import (
    DeliveryEvidence,
    GopherAction,
    GopherReason,
    run_gopher_tick,
)


# ---------------------------------------------------------------------------
# Fixtures: test scenarios sent to the model
# ---------------------------------------------------------------------------

FIXTURES: list[dict[str, Any]] = [
    # Fixture 1: Fresh unclaimed delivery
    {
        "name": "fresh_unclaimed",
        "system_prompt": (
            "You are a gopher/courier agent that monitors delivery status. "
            "Respond ONLY with a JSON object containing: action, reason, target_agent, "
            "channel_id, message, next_check_seconds, payload. "
            "Valid actions: ack_sender, wait, nudge_target, notify_human, record_observation, no_op. "
            "Valid reasons: recorded, unclaimed, claimed_no_activity, provider_slow, "
            "tool_waiting, suppressed, target_offline, unknown, callback_persisted. "
            "target_agent and channel_id must match the evidence. "
            "Do not target 'gopher' or 'courier' agents."
        ),
        "user_prompt": (
            'Delivery wake received: message_id=msg-fresh-01, '
            'delivery_id=d-001, target_agent=worker-alpha, '
            'channel_id=wake-general, status=unclaimed, '
            'gateway_span_ms=450.2, bridge_span_ms=2800.0, '
            'provider_timing_unavailable=false. '
            'What action do you take?'
        ),
        "evidence": DeliveryEvidence(
            message_id="msg-fresh-01",
            delivery_id="d-001",
            target_agent="worker-alpha",
            channel_id="wake-general",
            status="unclaimed",
            gateway_span_ms=450.2,
            bridge_span_ms=2800.0,
            provider_timing_unavailable=False,
        ),
        "expected_valid_action": GopherAction.ACK_SENDER,
    },
    # Fixture 2: Slow provider (provider_timing_unavailable)
    {
        "name": "provider_slow",
        "system_prompt": (
            "You are a gopher/courier agent. Respond ONLY with valid JSON matching "
            "the gopher action schema. Actions: ack_sender, wait, nudge_target, "
            "notify_human, record_observation, no_op."
        ),
        "user_prompt": (
            'Delivery stuck: message_id=msg-slow-02, '
            'delivery_id=d-002, target_agent=worker-beta, '
            'channel_id=wake-urgent, status=claimed_no_activity, '
            'gateway_span_ms=3200.0, bridge_span_ms=null, '
            'provider_timing_unavailable=true. '
            'What action do you take?'
        ),
        "evidence": DeliveryEvidence(
            message_id="msg-slow-02",
            delivery_id="d-002",
            target_agent="worker-beta",
            channel_id="wake-urgent",
            status="claimed_no_activity",
            gateway_span_ms=3200.0,
            bridge_span_ms=None,
            provider_timing_unavailable=True,
        ),
        "expected_valid_action": GopherAction.NUDGE_TARGET,
    },
    # Fixture 3: Callback already persisted
    {
        "name": "callback_persisted",
        "system_prompt": (
            "You are a gopher/courier agent. Respond ONLY with valid JSON matching "
            "the gopher action schema."
        ),
        "user_prompt": (
            'Delivery complete: message_id=msg-done-03, '
            'delivery_id=d-003, target_agent=worker-alpha, '
            'channel_id=wake-general, status=callback_persisted, '
            'gateway_span_ms=589.4, bridge_span_ms=3099.2, '
            'provider_timing_unavailable=false. '
            'What action do you take?'
        ),
        "evidence": DeliveryEvidence(
            message_id="msg-done-03",
            delivery_id="d-003",
            target_agent="worker-alpha",
            channel_id="wake-general",
            status="callback_persisted",
            gateway_span_ms=589.4,
            bridge_span_ms=3099.2,
            provider_timing_unavailable=False,
        ),
        "expected_valid_action": GopherAction.NO_OP,
    },
    # Fixture 4: Target offline
    {
        "name": "target_offline",
        "system_prompt": (
            "You are a gopher/courier agent. Respond ONLY with valid JSON matching "
            "the gopher action schema."
        ),
        "user_prompt": (
            'Delivery failed: message_id=msg-offline-04, '
            'delivery_id=d-004, target_agent=worker-gamma, '
            'channel_id=wake-critical, status=target_offline, '
            'gateway_span_ms=120.0, bridge_span_ms=null, '
            'provider_timing_unavailable=false, '
            'age_seconds=1800. What action do you take?'
        ),
        "evidence": DeliveryEvidence(
            message_id="msg-offline-04",
            delivery_id="d-004",
            target_agent="worker-gamma",
            channel_id="wake-critical",
            status="target_offline",
            gateway_span_ms=120.0,
            bridge_span_ms=None,
            provider_timing_unavailable=False,
            age_seconds=1800.0,
        ),
        "expected_valid_action": GopherAction.NUDGE_TARGET,
    },
    # Fixture 5: Long waiting delivery
    {
        "name": "stale_long_wait",
        "system_prompt": (
            "You are a gopher/courier agent. Respond ONLY with valid JSON matching "
            "the gopher action schema."
        ),
        "user_prompt": (
            'Delivery stale: message_id=msg-stale-05, '
            'delivery_id=d-005, target_agent=worker-delta, '
            'channel_id=wake-general, status=in_progress, '
            'gateway_span_ms=300.1, bridge_span_ms=5000.0, '
            'provider_timing_unavailable=false, '
            'age_seconds=900. What action do you take?'
        ),
        "evidence": DeliveryEvidence(
            message_id="msg-stale-05",
            delivery_id="d-005",
            target_agent="worker-delta",
            channel_id="wake-general",
            status="in_progress",
            gateway_span_ms=300.1,
            bridge_span_ms=5000.0,
            provider_timing_unavailable=False,
            age_seconds=900.0,
        ),
        "expected_valid_action": GopherAction.NUDGE_TARGET,
    },
]


# ---------------------------------------------------------------------------
# Fake model: returns hardcoded valid JSON for offline testing
# ---------------------------------------------------------------------------


def _fake_model_response(fixture: dict[str, Any]) -> dict[str, Any]:
    """Return a valid action proposal for the given fixture.

    Used in offline/fake mode to verify the schema eval path itself.
    """
    evidence = fixture["evidence"]
    expected_action = fixture["expected_valid_action"]
    return {
        "action": expected_action.value,
        "reason": "recorded",
        "target_agent": evidence.target_agent,
        "channel_id": evidence.channel_id,
        "message": f"Processing {evidence.delivery_id}: {expected_action.value}",
        "next_check_seconds": 60,
        "payload": {},
    }


# ---------------------------------------------------------------------------
# Live model: calls OpenAI-compatible endpoint
# ---------------------------------------------------------------------------


# Fixture-level results
@dataclass
class EvalResult:
    fixture_name: str
    model_raw: str
    model_json: dict[str, Any] | None
    parse_error: str | None
    packet_action: str
    packet_reason: str
    schema_valid: bool
    validation_errors: list[str]
    latency_ms: float
    expected_action: str
    action_match: bool


def _call_openai(
    base_url: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    timeout: int = 30,
) -> dict[str, Any] | None:
    """Call an OpenAI-compatible endpoint and return parsed JSON."""
    import http.client
    import urllib.parse

    parsed = urllib.parse.urlparse(base_url)
    path = parsed.path.rstrip("/") or "/v1/chat/completions"
    if not path.endswith("/chat/completions"):
        path = path.rstrip("/") + "/chat/completions"

    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 1024,
    })

    headers = {
        "Content-Type": "application/json",
    }

    host: str = parsed.hostname or "localhost"
    conn = http.client.HTTPConnection(
        host,
        parsed.port or 80,
        timeout=timeout,
    )
    try:
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            print(f"  [WARN] HTTP {resp.status}: {raw.decode(errors='replace')[:200]}", file=sys.stderr)
            return None
        data = json.loads(raw.decode())
        msg = data.get("choices", [{}])[0].get("message", {})
        content = (msg.get("content") or "").strip()
        reasoning = (msg.get("reasoning_content") or "").strip()

        # Try to find JSON in content first, then reasoning_content
        raw_text = content if content else reasoning
        if not raw_text:
            return None

        # Handle markdown code fences
        if raw_text.startswith("```"):
            lines = raw_text.splitlines()
            raw_text = "\n".join(line for line in lines if not line.startswith("```"))

        # Try direct JSON parse
        try:
            return json.loads(raw_text)
        except json.JSONDecodeError:
            pass

        # Try to find a JSON object via regex (handles JSON embedded in reasoning prose)
        import re
        # Match {...} from first { to matching }
        brace_depth = 0
        json_start = -1
        for i, ch in enumerate(raw_text):
            if ch == '{':
                if brace_depth == 0:
                    json_start = i
                brace_depth += 1
            elif ch == '}':
                brace_depth -= 1
                if brace_depth == 0 and json_start >= 0:
                    candidate = raw_text[json_start:i+1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        pass
                    json_start = -1

        return None
    except (json.JSONDecodeError, ConnectionError, TimeoutError, OSError) as e:
        print(f"  [ERROR] API call failed: {e}", file=sys.stderr)
        return None
    finally:
        conn.close()


def _call_ollama(
    base_url: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    timeout: int = 30,
) -> dict[str, Any] | None:
    """Call an Ollama chat endpoint and return parsed JSON."""
    import http.client
    import urllib.parse

    parsed = urllib.parse.urlparse(base_url)
    host: str = parsed.hostname or "localhost"
    path = parsed.path.rstrip("/") or "/api/chat"
    if not path.endswith("/chat"):
        path = path.rstrip("/") + "/api/chat"

    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "options": {
            "temperature": 0.1,
            "num_predict": 1024,
        },
        "stream": False,
    })

    headers = {"Content-Type": "application/json"}
    conn = http.client.HTTPConnection(
        host,
        parsed.port or 80,
        timeout=timeout,
    )
    try:
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status != 200:
            print(f"  [WARN] HTTP {resp.status}: {raw.decode(errors='replace')[:200]}", file=sys.stderr)
            return None
        data = json.loads(raw.decode())
        msg = data.get("message", {})
        content = (msg.get("content") or msg.get("reasoning_content") or "")
        if not content:
            return None
        content = content.strip()
        if content.startswith("```"):
            lines = content.splitlines()
            content = "\n".join(line for line in lines if not line.startswith("```"))
        return json.loads(content)
    except (json.JSONDecodeError, ConnectionError, TimeoutError, OSError) as e:
        print(f"  [ERROR] Ollama call failed: {e}", file=sys.stderr)
        return None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Main eval runner
# ---------------------------------------------------------------------------


def run_offline_eval() -> list[EvalResult]:
    """Run evaluation in fake/offline mode with canned model responses."""
    results: list[EvalResult] = []

    for fix in FIXTURES:
        start = time.perf_counter()
        model_json = _fake_model_response(fix)
        latency = (time.perf_counter() - start) * 1000

        evidence = fix["evidence"]
        packet = run_gopher_tick(evidence=evidence, model_raw_json=model_json)
        expected = fix["expected_valid_action"]

        results.append(EvalResult(
            fixture_name=fix["name"],
            model_raw=json.dumps(model_json),
            model_json=model_json,
            parse_error=None,
            packet_action=packet.fsm_action.value,
            packet_reason=packet.fsm_reason.value,
            schema_valid=packet.schema_valid,
            validation_errors=list(packet.validation_errors),
            latency_ms=latency,
            expected_action=expected.value,
            action_match=packet.fsm_action == expected,
        ))

    return results


def run_live_eval(
    base_url: str,
    model: str,
    endpoint_type: str = "openai",
) -> list[EvalResult]:
    """Run evaluation against a live local model endpoint."""
    results: list[EvalResult] = []

    for fix in FIXTURES:
        sys_prompt = fix["system_prompt"]
        user_prompt = fix["user_prompt"]
        evidence = fix["evidence"]
        expected = fix["expected_valid_action"]

        print(f"  Evaluating '{fix['name']}'...", end=" ", flush=True)

        start = time.perf_counter()

        if endpoint_type == "ollama":
            model_json = _call_ollama(base_url, model, sys_prompt, user_prompt)
        else:
            model_json = _call_openai(base_url, model, sys_prompt, user_prompt)

        latency = (time.perf_counter() - start) * 1000

        if model_json is None:
            print(f"FAIL (model returned nothing)")
            results.append(EvalResult(
                fixture_name=fix["name"],
                model_raw="",
                model_json=None,
                parse_error="Model returned no response",
                packet_action="record_observation",
                packet_reason="unknown",
                schema_valid=False,
                validation_errors=["No model response"],
                latency_ms=latency,
                expected_action=expected.value,
                action_match=False,
            ))
            continue

        # Run through gopher FSM
        packet = run_gopher_tick(evidence=evidence, model_raw_json=model_json)

        match = packet.fsm_action == expected
        status = "OK" if match else f"MISMATCH (got {packet.fsm_action.value})"
        print(f"{status} [{latency:.0f}ms]")

        results.append(EvalResult(
            fixture_name=fix["name"],
            model_raw=json.dumps(model_json),
            model_json=model_json,
            parse_error=None,
            packet_action=packet.fsm_action.value,
            packet_reason=packet.fsm_reason.value,
            schema_valid=packet.schema_valid,
            validation_errors=list(packet.validation_errors),
            latency_ms=latency,
            expected_action=expected.value,
            action_match=match,
        ))

    return results


def print_summary(results: list[EvalResult], mode: str):
    """Print a structured summary of evaluation results."""
    total = len(results)
    valid = sum(1 for r in results if r.schema_valid)
    matches = sum(1 for r in results if r.action_match)
    latencies = [r.latency_ms for r in results]

    print()
    print("=" * 60)
    print(f"  GOPHER MODEL EVALUATION SUMMARY ({mode})")
    print("=" * 60)
    print(f"  Total fixtures:     {total}")
    print(f"  Schema valid:       {valid}/{total} ({100 * valid / max(total, 1):.0f}%)")
    print(f"  Expected action:    {matches}/{total} ({100 * matches / max(total, 1):.0f}%)")
    if latencies:
        print(f"  Latency (ms):       min={min(latencies):.0f}  max={max(latencies):.0f}  "
              f"mean={sum(latencies) / len(latencies):.0f}")
    print()

    # Action distribution
    action_counts: dict[str, int] = {}
    for r in results:
        action_counts[r.packet_action] = action_counts.get(r.packet_action, 0) + 1
    print("  Action distribution:")
    for action, count in sorted(action_counts.items()):
        print(f"    {action}: {count}")
    print()

    # Invalid reasons
    invalid = [r for r in results if not r.schema_valid]
    if invalid:
        print("  Validation failures:")
        for r in invalid:
            print(f"    [{r.fixture_name}] {', '.join(r.validation_errors)}")
        print()

    # Errors
    errors = [r for r in results if r.parse_error]
    if errors:
        print("  Parse/API errors:")
        for r in errors:
            print(f"    [{r.fixture_name}] {r.parse_error}")
        print()

    # Per-fixture detail
    print("  Per-fixture details:")
    for r in results:
        icon = "OK" if r.action_match else "XX"
        valid_icon = "V" if r.schema_valid else "I"
        print(f"    [{icon}/{valid_icon}] {r.fixture_name:20s} action={r.packet_action:20s} "
              f"expected={r.expected_action:20s} latency={r.latency_ms:7.0f}ms")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate gopher model against action schema",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        default=False,
        help="Run in offline/fake mode (no model calls)",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("GOPHER_EVAL_BASE_URL", ""),
        help="Base URL for OpenAI-compatible endpoint",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("GOPHER_EVAL_MODEL", ""),
        help="Model name to use",
    )
    parser.add_argument(
        "--endpoint-type",
        choices=["openai", "ollama"],
        default="openai",
        help="Endpoint protocol type",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write detailed results JSON to this path",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.offline:
        print("\n  Running gopher model evaluation in OFFLINE/FAKE mode...\n")
        results = run_offline_eval()
        print_summary(results, "OFFLINE")
    else:
        if not args.base_url:
            print(
                "ERROR: --base-url required for live mode. "
                "Use --offline for fake mode, or set GOPHER_EVAL_BASE_URL.",
                file=sys.stderr,
            )
            return 1
        if not args.model:
            print(
                "ERROR: --model required for live mode. "
                "Use --offline for fake mode, or set GOPHER_EVAL_MODEL.",
                file=sys.stderr,
            )
            return 1
        print(f"\n  Running gopher model evaluation (LIVE mode)")
        print(f"  Endpoint: {args.base_url}")
        print(f"  Model:    {args.model}")
        print(f"  Type:     {args.endpoint_type}\n")
        results = run_live_eval(args.base_url, args.model, args.endpoint_type)
        print_summary(results, "LIVE")

    # Write output if requested
    if args.output:
        output_data = {
            "mode": "offline" if args.offline else "live",
            "base_url": args.base_url,
            "model": args.model,
            "results": [
                {
                    "fixture_name": r.fixture_name,
                    "packet_action": r.packet_action,
                    "packet_reason": r.packet_reason,
                    "schema_valid": r.schema_valid,
                    "validation_errors": r.validation_errors,
                    "latency_ms": r.latency_ms,
                    "expected_action": r.expected_action,
                    "action_match": r.action_match,
                    "parse_error": r.parse_error,
                    "model_raw": r.model_raw,
                }
                for r in results
            ],
        }
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(output_data, indent=2))
        print(f"  Results written to: {output_path.resolve()}\n")

    return 0 if all(r.schema_valid for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())

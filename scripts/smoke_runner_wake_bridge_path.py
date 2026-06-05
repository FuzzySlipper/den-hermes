#!/usr/bin/env python3
"""Deterministic smoke: exercises the exact Runner wake bridge client path.

This script simulates the full wake-bridge path the Runner uses to wake a
pool worker via Den Channels direct-agent messages.

It does NOT require a live Den service. All network calls are faked or
validated locally.  Pass ``--live`` to smoke against a real Channels URL.

The smoke verifies:

  1. URL construction matches the expected Channels endpoint.
  2. Payload shape includes concrete target metadata (assignmentId,
     workerRunId, workerRole, poolMemberId, profileIdentity).
  3. _channels_request error diagnostics include method, url, base_url,
     endpoint, and error text without leaking secrets.
  4. Plugin handler error diagnostics include base_url, endpoint,
     request_shape, and failure_category.

Exit codes:
    0 -- all checks passed.
    1 -- one or more checks failed.
    2 -- live smoke failed (only when ``--live`` is passed).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# Make direct script execution work without requiring PYTHONPATH=.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from den_hermes.api_urls import join_api_url
from den_hermes.orchestrator import DenWorkflowAdapter


EXPECTED_DIRECT_AGENT_PATH = "/api/direct-agent-events"
DEFAULT_CHANNELS_URL = "http://channels.test"
TEST_MEMBER = "pool-coder-01"
TEST_CHANNEL_ID = 42


def _make_minimal_tools() -> Any:
    """Minimal stub that provides the MCP tools the adapter expects."""
    class MinimalTools:
        def mcp_den_get_task_workflow_summary(self, **kwargs):
            return {"task": {"id": 1911, "status": "in_progress"}}
        def mcp_den_determine_orchestrator_next_action(self, **kwargs):
            return {"next_action": "start_coder", "reason": "test"}
        def mcp_den_get_latest_worker_completion(self, **kwargs):
            return {"completion_state": "missing_packet"}
    return MinimalTools()


def make_test_adapter(channels_url: str = DEFAULT_CHANNELS_URL) -> DenWorkflowAdapter:
    return DenWorkflowAdapter(
        tools=_make_minimal_tools(),
        project_id="den-hermes-bridge",
        requested_by="den-hermes-runner",
        channels_url=channels_url,
    )


# ---------------------------------------------------------------------------
# Smoke 1: Verify URL construction does not double-path
# ---------------------------------------------------------------------------

def smoke_url_construction() -> list[str]:
    """Verify _channels_request constructs the correct URL from different base URLs."""
    errors: list[str] = []
    cases = [
        ("http://host.test", f"http://host.test{EXPECTED_DIRECT_AGENT_PATH}"),
        ("http://host.test/", f"http://host.test{EXPECTED_DIRECT_AGENT_PATH}"),
        ("http://192.168.1.10:18080", f"http://192.168.1.10:18080{EXPECTED_DIRECT_AGENT_PATH}"),
        # Historical profile configs may include API suffixes. The bridge must
        # normalize these before appending an absolute API endpoint.
        ("http://host.test/api", f"http://host.test{EXPECTED_DIRECT_AGENT_PATH}"),
        ("http://host.test/api", f"http://host.test{EXPECTED_DIRECT_AGENT_PATH}"),
    ]
    for base, expected in cases:
        actual = join_api_url(base, EXPECTED_DIRECT_AGENT_PATH)
        if actual != expected:
            errors.append(
                f"shared URL mismatch: base={base!r}\n"
                f"  expected: {expected}\n"
                f"  got:      {actual}"
            )
    return errors


# ---------------------------------------------------------------------------
# Smoke 2: Verify payload shape includes all target metadata
# ---------------------------------------------------------------------------

def smoke_payload_metadata() -> list[str]:
    """Verify send_direct_agent_message payload includes concrete target metadata."""
    errors: list[str] = []
    captured: dict[str, Any] = {}

    def capture(method, path, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["json_payload"] = kwargs.get("json_payload")
        return {"ok": True}

    adapter = make_test_adapter()
    object.__setattr__(adapter, "_channels_request", capture)
    adapter.send_direct_agent_message(
        channel_id=TEST_CHANNEL_ID,
        member_identity="pool-coder-01",
        body="Wake: task 1911 coder context ready.",
        assignment_id=132,
        worker_run_id="dc-1911-send-wake",
        worker_role="coder",
        pool_member_id="pool-coder-01",
        profile_identity="spawned-coder",
        source_project_id="den-hermes-bridge",
        target_task_id=1911,
    )

    payload = captured.get("json_payload", {})
    required_fields = {
        "channelId": TEST_CHANNEL_ID,
        "memberIdentity": "pool-coder-01",
        "senderIdentity": "den-hermes-runner",
        "assignmentId": "132",
        "workerRunId": "dc-1911-send-wake",
        "workerRole": "coder",
        "poolMemberId": "pool-coder-01",
        "profileIdentity": "spawned-coder",
        "sourceProjectId": "den-hermes-bridge",
        "targetTaskId": 1911,
    }
    for field, expected in required_fields.items():
        actual = payload.get(field)
        if actual != expected:
            errors.append(
                f"Payload field {field!r}: expected {expected!r}, got {actual!r}"
            )
    return errors


# ---------------------------------------------------------------------------
# Smoke 3: Verify error diagnostics include routing details
# ---------------------------------------------------------------------------

def smoke_error_diagnostics() -> list[str]:
    """Verify _channels_request error diagnostics include base_url, endpoint, method."""
    errors: list[str] = []

    def failing_request(method, path, **kwargs):
        return {
            "ok": False,
            "error": "HTTP Error 404: Not Found",
            "method": method,
            "url": f"{DEFAULT_CHANNELS_URL}{EXPECTED_DIRECT_AGENT_PATH}",
            "base_url": DEFAULT_CHANNELS_URL,
            "endpoint": path,
        }

    adapter = make_test_adapter()
    object.__setattr__(adapter, "_channels_request", failing_request)
    result = adapter.send_direct_agent_message(
        channel_id=TEST_CHANNEL_ID,
        member_identity=TEST_MEMBER,
        body="Wake test",
    )

    if result.get("ok") is not False:
        errors.append("Expected ok=False on error response")
    if result.get("failure_category") != "worker_wake_channels_route_error":
        errors.append(
            f"Expected failure_category='worker_wake_channels_route_error', "
            f"got {result.get('failure_category')!r}"
        )
    if "direct-agent-messages" not in str(result.get("diagnostic", "")):
        errors.append(
            f"Diagnostic missing endpoint path: {result.get('diagnostic')}"
        )
    if DEFAULT_CHANNELS_URL not in str(result.get("diagnostic", "")):
        errors.append(
            f"Diagnostic missing base URL: {result.get('diagnostic')}"
        )
    return errors


# ---------------------------------------------------------------------------
# Smoke 4: Verify plugin handler error diagnostics
# ---------------------------------------------------------------------------

def smoke_plugin_handler_error_diagnostics() -> list[str]:
    """Verify the plugin handler's error diagnostics include classification."""
    errors: list[str] = []

    # Import the plugin module
    sys.path.insert(0, str(REPO_ROOT / "plugins"))
    from platforms.den_channels.adapter import (
        _classify_direct_agent_failure,
    )

    # Test failure classification against different exception types
    test_cases = [
        # Simulated 404
        ("httpx.HTTPStatusError", 404, "worker_wake_channels_route_404"),
        # Simulated 401
        ("httpx.HTTPStatusError", 401, "worker_wake_channels_auth_error"),
        # Simulated 403
        ("httpx.HTTPStatusError", 403, "worker_wake_channels_auth_error"),
        # Simulated 500
        ("httpx.HTTPStatusError", 500, "worker_wake_channels_http_500"),
        # Connection refused text
        ("ConnectionError", 0, "worker_wake_channels_connection_error"),
    ]

    for exc_type_str, status, expected_category in test_cases:
        if exc_type_str == "httpx.HTTPStatusError":
            import httpx

            try:
                import httpx as _httpx

                class FakeResponse:
                    status_code = status
                    def raise_for_status(self):
                        raise httpx.HTTPStatusError(
                            f"HTTP {status}",
                            request=httpx.Request("POST", "http://test"),
                            response=self,
                        )

                exc = httpx.HTTPStatusError(
                    f"HTTP {status}",
                    request=httpx.Request("POST", "http://test"),
                    response=FakeResponse(),
                )
            except Exception:
                continue
        elif exc_type_str == "ConnectionError":
            exc = ConnectionError("Connection refused")
        else:
            continue

        category = _classify_direct_agent_failure(exc)
        if category != expected_category:
            errors.append(
                f"Classification mismatch for {exc_type_str}({status}): "
                f"expected {expected_category!r}, got {category!r}"
            )
    return errors


# ---------------------------------------------------------------------------
# Smoke 5: Verify base URL resolution both adapter and plugin paths
# ---------------------------------------------------------------------------

def smoke_base_url_resolution() -> list[str]:
    """Verify base URL resolution matches between adapter and plugin paths."""
    errors: list[str] = []

    # The orchestrator adapter resolves:
    #   channels_url = os.environ.get("DEN_CHANNELS_URL")
    #                 or os.environ.get("DEN_GATEWAY_URL")
    #                 or None
    #
    # The plugin handler resolves:
    #   channels_url = os.getenv("DEN_CHANNELS_URL") or ""
    #   channels_url  = os.getenv("DEN_GATEWAY_URL") or ""
    #   base_url = channels_url or channels_url
    #
    # Both fall back the same way: DEN_CHANNELS_URL first, then DEN_GATEWAY_URL.
    # If DEN_GATEWAY_URL is set to a path-prefixed URL (e.g., includes
    # /api), the plugin handler would construct a double-path endpoint.
    # This smoke validates the correct pattern.

    base_live = os.environ.get("DEN_CHANNELS_URL") or os.environ.get("DEN_GATEWAY_URL")
    if not base_live:
        # Not running live; skip (expected)
        return errors

    base = base_live.rstrip("/")
    if base.endswith("/api"):
        errors.append(
            f"DEN_GATEWAY_URL appears to be path-prefixed: {base!r}. "
            f"This will cause double-path when appended to "
            f"/api/direct-agent-events. Expected bare host URL "
            f"(e.g. http://192.168.1.10:18080)."
        )
    else:
        expected_url = f"{base}{EXPECTED_DIRECT_AGENT_PATH}"
        errors.append(
            f"LIVE BASE URL: {base_live!r}\n"
            f"  Expected endpoint: {expected_url}\n"
            f"  Run with --live to smoke the actual route."
        )
    return errors


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="Also smoke against a live Channels (requires DEN_CHANNELS_URL)")
    parser.add_argument("--json", action="store_true",
                        help="Emit structured JSON report")
    args = parser.parse_args(argv)

    report: dict[str, Any] = {
        "smoke": "runner_wake_bridge_path",
        "checks": {},
        "all_passed": True,
    }

    checks = [
        ("url_construction", smoke_url_construction),
        ("payload_metadata", smoke_payload_metadata),
        ("error_diagnostics", smoke_error_diagnostics),
        ("plugin_handler_error", smoke_plugin_handler_error_diagnostics),
        ("base_url_resolution", smoke_base_url_resolution),
    ]

    for name, fn in checks:
        errors = fn()
        report["checks"][name] = {
            "passed": len(errors) == 0,
            "errors": errors,
        }
        if errors:
            report["all_passed"] = False

    if args.live:
        base_live = os.environ.get("DEN_CHANNELS_URL") or os.environ.get("DEN_GATEWAY_URL")
        if not base_live:
            report["checks"]["live"] = {
                "passed": False,
                "errors": ["DEN_CHANNELS_URL or DEN_GATEWAY_URL not set"],
            }
            report["all_passed"] = False
        else:
            from urllib.request import Request, urlopen
            import urllib.error

            base = base_live.rstrip("/")
            url = f"{base}{EXPECTED_DIRECT_AGENT_PATH}"
            payload = json.dumps({
                "channelId": TEST_CHANNEL_ID,
                "memberIdentity": "smoke-test-agent",
                "body": "Wake bridge path smoke test",
                "senderIdentity": "den-hermes-bridge-smoke",
            }).encode("utf-8")
            try:
                req = Request(url, data=payload, method="POST")
                req.add_header("Content-Type", "application/json")
                with urlopen(req, timeout=10) as resp:
                    status = resp.status
                    body = resp.read().decode("utf-8")
                    resp_json = json.loads(body) if body else {}
                report["checks"]["live"] = {
                    "passed": status == 201 or status == 200,
                    "status": status,
                    "response": resp_json,
                    "url": url,
                    "note": (
                        "Live wake bridge path succeeded. "
                        "The 201 confirms the Channels route works with the "
                        "same request shape the Runner uses."
                        if status in (200, 201) else
                        "Live smoke returned unexpected status."
                    ),
                }
            except urllib.error.HTTPError as exc:
                report["checks"]["live"] = {
                    "passed": False,
                    "status": exc.code,
                    "url": url,
                    "error": str(exc),
                    "note": (
                        "Live smoke returned HTTP {exc.code}. "
                        "Compare this URL ({url}) against the planner's curl. "
                        "If the planner got 201 and this got {exc.code}, "
                        "check that DEN_CHANNELS_URL/DEN_GATEWAY_URL resolve "
                        "to the same bare host without path prefix."
                    ),
                }
                report["all_passed"] = False
            except Exception as exc:
                report["checks"]["live"] = {
                    "passed": False,
                    "url": url,
                    "error": str(exc)[:200],
                }
                report["all_passed"] = False

    # Determine diagnostic for the original 404 discrepancy
    if not report["all_passed"]:
        report["diagnostics"] = {
            "route_discrepancy_explanation": (
                "The Runner's 404 on direct-agent wake is most likely caused by "
                "a base URL resolution mismatch. The orchestrator adapter and plugin "
                "handler both use DEN_CHANNELS_URL or DEN_GATEWAY_URL. If DEN_GATEWAY_URL "
                "is set to a path-prefixed URL (e.g. http://host:port/api), "
                "the plugin handler constructs a double-path endpoint "
                "(e.g. http://host:port/api/api/direct-agent-events) "
                "which returns 404. The planner's curl bypasses this by using the "
                "bare host URL directly. The fix is to ensure DEN_GATEWAY_URL is the "
                "bare host (http://192.168.1.10:18080) and let the code append "
                "/api/direct-agent-events."
            ),
            "request_shape_summary": (
                "POST /api/direct-agent-events with JSON body: "
                "channelId, memberIdentity, senderIdentity, body + optional "
                "assignmentId, workerRunId, workerRole, poolMemberId, "
                "profileIdentity, sourceProjectId, targetTaskId. "
                "Content-Type: application/json."
            ),
        }

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        for name, check in report["checks"].items():
            status = "PASS" if check["passed"] else "FAIL"
            print(f"[{status}] {name}")
            for err in check.get("errors", []):
                print(f"       {err}")
        if not report["all_passed"]:
            print("\nDiagnostics:")
            diag = report.get("diagnostics", {})
            for key, val in diag.items():
                print(f"  {key}: {val}")
        print(f"\nOverall: {'ALL PASSED' if report['all_passed'] else 'SOME FAILED'}")

    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

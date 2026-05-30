#!/usr/bin/env python3
"""HTTP service for the Vision Analyzer capability.

Provides:
  POST /vision/analyze-image  — accepts executor request envelope JSON, returns response
  GET /health                 — health check with capability id

Uses stdlib http.server (no external dependencies). Configurable via env/CLI.

Usage:
    python scripts/serve_vision_analyzer.py
    python scripts/serve_vision_analyzer.py --host 0.0.0.0 --port 8080
    VISION_ANALYZER_HOST=0.0.0.0 VISION_ANALYZER_PORT=9090 python scripts/serve_vision_analyzer.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

# Add repo root to path for imports
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from den_hermes.vision_analyzer import (
    CAPABILITY_ID,
    CAPABILITY_VERSION,
    ExecutorRequest,
    ResponseEnvelope,
    ResponseStatus,
    _error_response,
    execute_vision_analysis,
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_HOST = os.environ.get("VISION_ANALYZER_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("VISION_ANALYZER_PORT", "0")) or 8080


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------


class VisionAnalyzerHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the vision analyzer service."""

    # Class-level override for fake analyzer mode
    use_fake_analyzer: bool = True

    # Mapping: dataclass snake_case field -> Core camelCase response key
    _RESPONSE_KEY_MAP: dict[str, str] = {
        "status": "status",
        "output_summary": "outputSummary",
        "output": "output",
        "output_artifact_refs": "outputArtifactRefs",
        "model": "model",
        "timings_ms": "timingsMs",
        "cost": "cost",
        "metadata": "metadata",
    }

    # Request fields that Core sends as camelCase (within request body)
    _REQUEST_CAMEL_MAP: dict[str, str] = {
        "imageRef": "image_ref",
        "includeOcr": "include_ocr",
        "includeRegions": "include_regions",
        "outputDetail": "output_detail",
        "localeHint": "locale_hint",
        "uiContext": "ui_context",
    }

    # Safety fields that Core sends as camelCase
    _SAFETY_CAMEL_MAP: dict[str, str] = {
        "visibleWritesAllowed": "visible_writes_allowed",
        "allowImageUrls": "allow_image_urls",
        "allowResourceRefs": "allow_resource_refs",
    }

    @staticmethod
    def _snake_to_camel(name: str) -> str:
        """Convert snake_case identifier to camelCase."""
        parts = name.split("_")
        return parts[0] + "".join(p.capitalize() for p in parts[1:])

    @staticmethod
    def _get_field(body: dict[str, Any], snake_name: str, default: Any = None) -> Any:
        """Get a field from body, trying both camelCase and snake_case."""
        camel_name = VisionAnalyzerHandler._snake_to_camel(snake_name)
        if camel_name in body:
            return body[camel_name]
        return body.get(snake_name, default)

    @staticmethod
    def _normalize_request(req_body: dict[str, Any]) -> dict[str, Any]:
        """Normalize request fields: remap camelCase to snake_case for internal use."""
        result: dict[str, Any] = {}
        for key, val in req_body.items():
            mapped = VisionAnalyzerHandler._REQUEST_CAMEL_MAP.get(key, key)
            result[mapped] = val
        return result

    @staticmethod
    def _normalize_safety(safety: dict[str, Any]) -> dict[str, Any]:
        """Normalize safety fields: remap camelCase to snake_case."""
        result: dict[str, Any] = {}
        for key, val in safety.items():
            mapped = VisionAnalyzerHandler._SAFETY_CAMEL_MAP.get(key, key)
            result[mapped] = val
        return result

    def do_GET(self) -> None:
        """Handle GET requests."""
        if self.path == "/health":
            self._send_json(
                200,
                {
                    "status": "ok",
                    "capability_id": CAPABILITY_ID,
                    "version": CAPABILITY_VERSION,
                    "handler": "vision-analyzer-http",
                },
            )
        else:
            self._send_json(404, {"error": "Not found", "path": self.path})

    def do_POST(self) -> None:
        """Handle POST requests."""
        if self.path == "/vision/analyze-image":
            self._handle_analyze()
        else:
            self._send_json(404, {"error": "Not found", "path": self.path})

    def _handle_analyze(self) -> None:
        """Parse executor request envelope, validate, execute, return response."""
        # Read body
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length == 0:
            self._send_json(
                400,
                _error_response(
                    status=ResponseStatus.INVALID_REQUEST.value,
                    output_summary="Empty request body",
                    output_data={"error": "Request body is required"},
                ),
            )
            return

        raw_body = self.rfile.read(content_length)

        # Parse JSON
        try:
            body: dict[str, Any] = json.loads(raw_body)
        except json.JSONDecodeError as e:
            self._send_json(
                400,
                _error_response(
                    status=ResponseStatus.INVALID_REQUEST.value,
                    output_summary="Invalid JSON in request body",
                    output_data={"error": f"JSON parse error: {e}"},
                ),
            )
            return

        # Build executor request with dual camelCase / snake_case support
        try:
            caller = self._get_field(body, "caller", "")
            if isinstance(caller, dict):
                # Core sends caller as object with agent/project_id/task_id
                # Preserve the object in ExecutorRequest
                pass

            deadline_val = self._get_field(body, "deadline_utc", 0.0)
            # Core sends ISO-8601 string; prototype sends float
            # ExecutorRequest accepts either type

            # Normalize request sub-fields (imageRef -> image_ref, etc.)
            raw_request: dict[str, Any] = self._get_field(body, "request", {})
            normalized_request = self._normalize_request(raw_request)

            # Normalize safety sub-fields (visibleWritesAllowed -> visible_writes_allowed)
            raw_safety: dict[str, Any] = self._get_field(body, "safety", {})
            normalized_safety = self._normalize_safety(raw_safety)

            executor_req = ExecutorRequest(
                invocation_id=self._get_field(body, "invocation_id", ""),
                capability_id=self._get_field(body, "capability_id", ""),
                capability_version=self._get_field(body, "capability_version"),
                caller=caller,
                side_effect_level=self._get_field(body, "side_effect_level", ""),
                deadline_utc=deadline_val,
                request=normalized_request,
                safety=normalized_safety,
            )
        except (ValueError, TypeError) as e:
            self._send_json(
                400,
                _error_response(
                    status=ResponseStatus.INVALID_REQUEST.value,
                    output_summary="Failed to parse executor request",
                    output_data={"error": str(e)},
                ),
            )
            return

        # Execute
        response = execute_vision_analysis(
            executor_req,
            use_fake_analyzer=self.use_fake_analyzer,
        )

        # Map status to HTTP status code
        http_status = _status_to_http(response.status)
        self._send_json(http_status, response)

    def _send_json(
        self, http_status: int, payload: ResponseEnvelope | dict[str, Any]
    ) -> None:
        """Send a JSON response."""
        if isinstance(payload, ResponseEnvelope):
            # Serialize with Core camelCase field names
            data = {}
            for snake_field, camel_field in self._RESPONSE_KEY_MAP.items():
                data[camel_field] = getattr(payload, snake_field)
        else:
            data = payload

        body_bytes = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(http_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.send_header("X-Capability-Id", CAPABILITY_ID)
        self.end_headers()
        self.wfile.write(body_bytes)

    def log_message(self, format: str, *args: Any) -> None:
        """Override to suppress default logging or format nicely."""
        sys.stderr.write(f"[vision-analyzer] {args[0]} {args[1]} {args[2]}\n")


def _status_to_http(status: str) -> int:
    """Map response status to HTTP status code."""
    if status == ResponseStatus.COMPLETED.value:
        return 200
    if status in (
        ResponseStatus.INVALID_REQUEST.value,
        ResponseStatus.SAFETY_REJECTED.value,
    ):
        return 400
    if status == ResponseStatus.MODEL_ERROR.value:
        return 502
    if status == ResponseStatus.TIMEOUT.value:
        return 504
    return 500


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Vision Analyzer capability HTTP service"
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"Host to bind to (default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Port to listen on (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--real-model",
        action="store_true",
        help="Enable real model execution path (requires external endpoint config)",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    VisionAnalyzerHandler.use_fake_analyzer = not args.real_model

    server = HTTPServer((args.host, args.port), VisionAnalyzerHandler)
    print(
        f"Vision Analyzer service starting on http://{args.host}:{args.port}",
        file=sys.stderr,
    )
    print(f"  Capability: {CAPABILITY_ID}", file=sys.stderr)
    print(f"  Mode: {'real model' if args.real_model else 'fake analyzer'}", file=sys.stderr)
    print(f"  (No secrets printed)", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...", file=sys.stderr)
        server.shutdown()


if __name__ == "__main__":
    main()

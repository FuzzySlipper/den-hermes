"""Shared URL helpers for Den HTTP API clients.

The Bridge has both orchestration-side stdlib clients and Hermes plugin-side
httpx clients.  Keep API-prefix normalization here so direct-agent wake paths do
not drift while still avoiding a dependency on either transport stack.
"""

from __future__ import annotations


def join_api_url(base_url: str, path: str) -> str:
    """Join an operator-configured Den API base URL to an absolute API path.

    Older profile configs have used bare origins (``http://host:18080``) and
    API-prefixed values (``http://host:18080/api`` or ``.../api/gateway``).
    Callers pass absolute API paths, so strip common API suffixes before joining
    to avoid double-path 404s such as
    ``/api/gateway/api/gateway/direct-agent-messages``.
    """
    normalized = base_url.rstrip("/")
    for suffix in ("/api/gateway", "/api"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    return f"{normalized}{path}"

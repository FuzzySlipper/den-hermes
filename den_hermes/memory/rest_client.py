from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping

from den_hermes.memory.config import DenMemoryConfig
from den_hermes.memory.errors import DenUnavailableError


@dataclass(frozen=True)
class DenMemoryRestClient:
    """Thin Den Core REST client for memory endpoints.

    Authentication is optional. When a token is resolved by the config both
    ``Authorization: Bearer *** and ``X-Den-Service-Token: <token>`` are
    injected on every request.  If no token is available the client operates in
    unauthenticated mode (appropriate for loopback / local dev).
    """

    config: DenMemoryConfig
    _token: str | None = field(init=False, repr=False, compare=False, hash=False)

    def __post_init__(self) -> None:
        # Cache the resolved token so the client snapshot is stable.  Because the
        # dataclass is frozen we mutate via ``object.__setattr__``.
        object.__setattr__(self, "_token", self.config.resolve_token())

    def _build_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Den-Project-Id": self.config.project_id,
            "X-Den-Requested-By": self.config.requested_by,
            "X-Den-Run-Id": self.config.run_id or "",
            "X-Den-Role": self.config.role,
        }
        token = self._token
        if token:
            headers["Authorization"] = f"Bearer {token}"
            headers["X-Den-Service-Token"] = token
        return headers

    def _build_request(
        self,
        method: str,
        path: str,
        *,
        json_payload: Mapping[str, Any] | None = None,
    ) -> tuple[str, str, dict[str, str], str | None]:
        """Prepare a request without sending it.

        Returns ``(method, url, headers, body)`` so tests can inspect the
        composed surface without requiring a live Den Core instance.
        """
        url = self.config.base_url.rstrip("/") + path
        headers = self._build_headers()
        body = json.dumps(json_payload, default=str) if json_payload is not None else None
        return method, url, headers, body

    def request(
        self,
        method: str,
        path: str,
        *,
        json_payload: Mapping[str, Any] | None = None,
    ) -> tuple[int, Any]:
        """Send a request and return ``(status_code, parsed_json)``.

        Raises ``DenUnavailableError`` on network/connection failures.
        Raises ``urllib.error.HTTPError`` on HTTP 4xx/5xx responses.
        """
        method, url, headers, body = self._build_request(method, path, json_payload=json_payload)
        req = urllib.request.Request(url, method=method, headers=headers)
        if body is not None:
            req.data = body.encode("utf-8")
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8")
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise DenUnavailableError(
                        f"Den Core at {url} returned non-JSON: {exc}"
                    ) from exc
                return resp.status, payload
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as exc:
            raise DenUnavailableError(
                f"Den Core REST unreachable at {url}: {exc}"
            ) from exc

    def __repr__(self) -> str:
        token_hint = "[REDACTED]" if self._token else "None"
        return (
            f"DenMemoryRestClient(config={self.config!r}, "
            f"token={token_hint})"
        )

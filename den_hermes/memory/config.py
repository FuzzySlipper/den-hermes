from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

_SECRETISH_PATTERN = re.compile(
    r"(?i)(bearer\s+\S+|sk-[a-z0-9_.-]{8,}|api[_-]?key|auth[_-]?token|\btoken\b|authorization|password|secret)"
)


def _redact(value: str) -> str:
    return _SECRETISH_PATTERN.sub("[REDACTED]", value)


@dataclass(frozen=True)
class DenMemoryConfig:
    """Configuration for the Den Core REST memory client.

    The bearer token is never stored inline. It is resolved at runtime from the
    ``DEN_CORE_API_TOKEN`` environment variable or from the file at
    ``credential_path``.  When no token is available the client operates in
    unauthenticated mode, which is valid for loopback / local development.
    """

    base_url: str = "http://192.168.1.10:5000"
    timeout_seconds: int = 30
    retry_attempts: int = 2
    credential_path: str | None = None
    project_id: str = ""
    requested_by: str = ""
    run_id: str | None = None
    role: str = ""
    task_id: int | None = None

    def resolve_token(self) -> str | None:
        """Return the bearer token, or ``None`` if unauthenticated.

        Resolution order:
        1. ``DEN_CORE_API_TOKEN`` environment variable.
        2. Contents of the file at ``credential_path`` (whitespace stripped).
        3. ``None`` — unauthenticated mode.
        """
        env_token = os.environ.get("DEN_CORE_API_TOKEN")
        if env_token:
            return env_token.strip()
        if self.credential_path:
            path = Path(self.credential_path)
            if path.is_file():
                return path.read_text().strip()
        return None

    def __repr__(self) -> str:
        return self._redacted_repr()

    def __str__(self) -> str:
        return self._redacted_repr()

    def _redacted_repr(self) -> str:
        token = self.resolve_token()
        token_hint = "[REDACTED]" if token else "None"
        return (
            f"DenMemoryConfig(base_url={self.base_url!r}, "
            f"timeout_seconds={self.timeout_seconds}, "
            f"retry_attempts={self.retry_attempts}, "
            f"credential_path={self.credential_path!r}, "
            f"token={token_hint}, "
            f"project_id={self.project_id!r}, "
            f"requested_by={self.requested_by!r}, "
            f"run_id={self.run_id!r}, "
            f"role={self.role!r}, "
            f"task_id={self.task_id!r})"
        )

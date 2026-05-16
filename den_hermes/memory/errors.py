from __future__ import annotations


class MemoryConfigError(ValueError):
    """Raised when the Den memory provider configuration is invalid or unsafe."""


class DenCoreApiGapError(RuntimeError):
    """Raised when a required Den Core REST endpoint is missing or incompatible."""


class DenUnavailableError(RuntimeError):
    """Raised when Den Core REST is unreachable or returns persistent errors."""

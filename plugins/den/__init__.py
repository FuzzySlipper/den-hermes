"""Den-owned opt-in Hermes memory provider plugin.

This plugin adapts the Den-owned ``den_hermes.memory.provider`` implementation
to Hermes' ``MemoryProvider`` interface.  It is intentionally manual-only:
``den_memory.deny_auto_behavior`` must be true and all automatic lifecycle
hooks are no-ops for the guinea-pig rollout.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import yaml

# When this plugin is installed into a profile, the shared install root also
# carries the ``den_hermes`` package as a sibling of the plugin directory.
_SHARED_ROOT = Path(__file__).resolve().parent.parent
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))

from agent.memory_provider import MemoryProvider
from hermes_constants import get_hermes_home
from tools.registry import tool_error

from den_hermes.memory.config import DenMemoryConfig
from den_hermes.memory.errors import MemoryConfigError
from den_hermes.memory.provider import DenMemoryProvider


_DEN_READ_SPACE_SCHEMA = {
    "type": "string",
    "description": "Optional configured Den memory space, e.g. assistant:researcher or knowledge_base:den-memory-smoke.",
}


class HermesDenMemoryProvider(MemoryProvider):
    """Hermes memory-plugin adapter for the Den memory provider."""

    def __init__(self) -> None:
        self._provider: DenMemoryProvider | None = None
        self._raw_config: dict[str, Any] = {}
        self._session_id = ""

    @property
    def name(self) -> str:
        return "den"

    def _load_profile_config(self) -> dict[str, Any]:
        config_path = get_hermes_home() / "config.yaml"
        if not config_path.exists():
            return {}
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}

    def _den_cfg(self) -> dict[str, Any]:
        config = self._load_profile_config()
        raw = config.get("den_memory") or {}
        return raw if isinstance(raw, dict) else {}

    def is_available(self) -> bool:
        cfg = self._den_cfg()
        return bool(cfg.get("enabled") is True and cfg.get("deny_auto_behavior") is True)

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._session_id = session_id or ""
        cfg = self._den_cfg()
        if cfg.get("deny_auto_behavior") is not True:
            raise MemoryConfigError("den_memory.deny_auto_behavior must be true for the initial rollout")
        if cfg.get("enabled") is not True:
            raise MemoryConfigError("den_memory.enabled must be true when memory.provider=den")

        rest = cfg.get("rest") or {}
        if not isinstance(rest, dict):
            raise MemoryConfigError("den_memory.rest must be a mapping")

        profile = str(cfg.get("profile") or kwargs.get("agent_identity") or "").strip()
        project_id = str(cfg.get("project_id") or "den-hermes-bridge").strip()
        base_url = str(rest.get("base_url") or "").strip()
        if not profile:
            raise MemoryConfigError("den_memory.profile is required")
        if not base_url:
            raise MemoryConfigError("den_memory.rest.base_url is required")

        read_spaces = tuple(str(s) for s in (cfg.get("read_spaces") or []) if str(s).strip())
        write_spaces = tuple(str(s) for s in (cfg.get("write_spaces") or []) if str(s).strip())
        default_write_space = cfg.get("default_write_space")
        if not read_spaces:
            raise MemoryConfigError("den_memory.read_spaces must not be empty")
        if not write_spaces:
            raise MemoryConfigError("den_memory.write_spaces must not be empty")
        if not default_write_space:
            raise MemoryConfigError("den_memory.default_write_space is required")

        raw_task_id = cfg.get("task_id")
        try:
            task_id = int(raw_task_id) if raw_task_id not in (None, "") else None
        except (TypeError, ValueError):
            task_id = None
        den_config = DenMemoryConfig(
            base_url=base_url,
            project_id=project_id,
            credential_path=str(rest.get("credential_path") or "") or None,
            timeout_seconds=int(rest.get("timeout_seconds", 10)),
            retry_attempts=int(rest.get("retry_attempts", 1)),
            requested_by=profile,
            role=str(cfg.get("role") or profile),
            task_id=task_id,
            run_id=self._session_id,
        )
        self._raw_config = cfg
        self._provider = DenMemoryProvider(
            den_config,
            read_spaces=read_spaces,
            default_space=read_spaces[0],
            write_spaces=write_spaces,
            default_write_space=str(default_write_space),
        )

    def system_prompt_block(self) -> str:
        if not self._provider:
            return ""
        return (
            "Den long-term memory is available through explicit tools only. "
            "Automatic capture, prefetch, and session-end extraction are disabled for this rollout. "
            "Use den_search/den_recall/den_read to inspect configured spaces and den_store/den_update only when a durable, non-secret memory is intentionally worth saving."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        return None

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        return None

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "den_search",
                "description": "Search configured Den long-term memory spaces. Manual-only; no automatic prefetch.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query."},
                        "space": _DEN_READ_SPACE_SCHEMA,
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "den_recall",
                "description": "Recall Den long-term memory entries related to a query, optionally constrained to a configured space or tags.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Recall query."},
                        "space": _DEN_READ_SPACE_SCHEMA,
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 5},
                        "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags that must be verified on matching entries."},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "den_read",
                "description": "Read one Den memory by slug/key from configured read spaces.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "slug": {"type": "string", "description": "Memory slug/key to read."},
                        "space": _DEN_READ_SPACE_SCHEMA,
                    },
                    "required": ["slug"],
                },
            },
            {
                "name": "den_list_my_memories",
                "description": "List inspectable memories in configured read spaces.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "space": _DEN_READ_SPACE_SCHEMA,
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
                    },
                    "required": [],
                },
            },
            {
                "name": "den_store",
                "description": "Intentionally store a durable Den long-term memory in a configured write space. Do not store secrets or temporary task progress.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "content": {"type": "string"},
                        "space": _DEN_READ_SPACE_SCHEMA,
                        "slug": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "summary": {"type": "string"},
                    },
                    "required": ["title", "content"],
                },
            },
            {
                "name": "den_update",
                "description": "Replace an existing Den memory entry in a configured write space with intentional updated content.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "slug": {"type": "string"},
                        "content": {"type": "string"},
                        "space": _DEN_READ_SPACE_SCHEMA,
                        "tags": {"type": "array", "items": {"type": "string"}},
                        "summary": {"type": "string"},
                        "title": {"type": "string"},
                    },
                    "required": ["slug", "content"],
                },
            },
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        if not self._provider:
            return tool_error("Den memory provider is not initialized")
        try:
            if tool_name == "den_search":
                result = self._provider.den_search(
                    str(args.get("query") or ""),
                    space=args.get("space"),
                    limit=int(args.get("limit") or 10),
                )
            elif tool_name == "den_recall":
                result = self._provider.den_recall(
                    str(args.get("query") or ""),
                    space=args.get("space"),
                    limit=int(args.get("limit") or 5),
                    tags=args.get("tags"),
                )
            elif tool_name == "den_read":
                result = self._provider.den_read(str(args.get("slug") or ""), space=args.get("space"))
            elif tool_name == "den_list_my_memories":
                result = self._provider.den_list_my_memories(space=args.get("space"), limit=int(args.get("limit") or 50))
            elif tool_name == "den_store":
                result = self._provider.den_store(
                    str(args.get("title") or ""),
                    str(args.get("content") or ""),
                    space=args.get("space"),
                    slug=args.get("slug"),
                    tags=args.get("tags"),
                    summary=args.get("summary"),
                )
            elif tool_name == "den_update":
                result = self._provider.den_update(
                    str(args.get("slug") or ""),
                    str(args.get("content") or ""),
                    space=args.get("space"),
                    tags=args.get("tags"),
                    summary=args.get("summary"),
                    title=args.get("title"),
                )
            else:
                return tool_error(f"Unknown Den memory tool: {tool_name}")
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            return tool_error(f"Den memory tool '{tool_name}' failed: {exc}")


def register(ctx: Any) -> None:
    ctx.register_memory_provider(HermesDenMemoryProvider())

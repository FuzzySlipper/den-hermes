from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from den_hermes.memory.config import DenMemoryConfig
from den_hermes.memory.errors import DenUnavailableError, MemoryConfigError
from den_hermes.memory.rest_client import DenMemoryRestClient


@dataclass
class DenMemoryProvider:
    """Den-backed memory provider with explicit opt-in read tools.

    Automatic behavior (prefetch, queue_prefetch, sync_turn, etc.) is
    explicitly disabled in this initial super.  The model must call read
    tools manually.
    """

    config: DenMemoryConfig
    read_spaces: tuple[str, ...] = ()
    default_space: str | None = None
    write_spaces: tuple[str, ...] = ()
    default_write_space: str | None = None
    _client: DenMemoryRestClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._client = DenMemoryRestClient(self.config)

    # ------------------------------------------------------------------
    # Deferred / no-op hooks (advanced super #1454)
    # ------------------------------------------------------------------

    def prefetch(self) -> dict[str, Any]:
        """No-op: automatic pre-turn memory retrieval is forbidden in the initial super.

        Deferred to advanced super #1454.
        """
        return {
            "status": "noop",
            "reason": "prefetch deferred to advanced super #1454",
        }

    def queue_prefetch(self) -> dict[str, Any]:
        """No-op: background/async prefetch queue is deferred to advanced super #1454."""
        return {
            "status": "noop",
            "reason": "queue_prefetch deferred to advanced super #1454",
        }

    def sync_turn(self) -> dict[str, Any]:
        """No-op: automatic per-turn memory synchronization is deferred to advanced super #1454."""
        return {
            "status": "noop",
            "reason": "sync_turn deferred to advanced super #1454",
        }

    def on_session_end(self) -> dict[str, Any]:
        """No-op: automatic session-end memory write is deferred to advanced super #1454."""
        return {
            "status": "noop",
            "reason": "on_session_end deferred to advanced super #1454",
        }

    def on_pre_compress(self) -> dict[str, Any]:
        """No-op: automatic compression-triggered memory capture is deferred to advanced super #1454."""
        return {
            "status": "noop",
            "reason": "on_pre_compress deferred to advanced super #1454",
        }

    def on_memory_write(self) -> dict[str, Any]:
        """No-op: automatic interception of memory writes for mirroring is deferred to advanced super #1454."""
        return {
            "status": "noop",
            "reason": "on_memory_write mirroring deferred to advanced super #1454",
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_spaces(self, explicit_space: str | None) -> list[str]:
        if explicit_space is not None:
            if explicit_space not in self.read_spaces:
                raise PermissionError(
                    f"Space {explicit_space!r} not in configured read_spaces: {self.read_spaces}"
                )
            return [explicit_space]
        return list(self.read_spaces)

    def _resolve_write_space(self, explicit_space: str | None) -> str:
        if explicit_space is not None:
            if explicit_space not in self.write_spaces:
                raise PermissionError(
                    f"Space {explicit_space!r} not in configured write_spaces: {self.write_spaces}"
                )
            return explicit_space
        if self.default_write_space is not None:
            if self.default_write_space not in self.write_spaces:
                raise MemoryConfigError(
                    f"default_write_space {self.default_write_space!r} not in write_spaces"
                )
            return self.default_write_space
        raise MemoryConfigError("No default_write_space configured for this profile.")

    def _build_provenance(self) -> dict[str, Any]:
        return {
            "profile": self.config.requested_by,
            "session_id": self.config.run_id or "",
            "task_id": self.config.task_id,
            "project_id": self.config.project_id,
            "run_id": self.config.run_id or "",
            "role": self.config.role,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "agent-decided",
        }

    @staticmethod
    def _slugify(title: str) -> str:
        slug = re.sub(r"[^\w\s-]", "", title.lower())
        slug = re.sub(r"[\s]+", "-", slug)
        return slug.strip("-")

    @staticmethod
    def _core_safe(value: str) -> str:
        """Mirror Den Core's generated memory entry id normalization."""
        return re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower()).strip("-")

    @classmethod
    def _core_entry_id(cls, space: str, key: str) -> str:
        return f"memory-{cls._core_safe(space)}-{cls._core_safe(key)}"

    @classmethod
    def _core_memory_key(cls, user_space: str, slug: str) -> str:
        """Namespace a user-facing memory key for the live Core compatibility API.

        Current Den Core memory routes expose a fixed set of storage spaces and
        parse unknown dynamic spaces as ``project`` on search results. Store
        Hermes' richer profile/kb space in the key + metadata while using the
        project storage bucket, then filter/strip the prefix client-side.
        """
        return f"{cls._core_safe(user_space)}-{slug}"

    @classmethod
    def _core_storage_space(cls, _user_space: str) -> str:
        return "project"

    def _uses_core_compat_storage(self) -> bool:
        """Whether the configured endpoint is the live Den Core compatibility facade."""
        return "den-core-api" in self.config.base_url

    def _store_payload_fields(self, space: str, slug: str) -> tuple[str, str]:
        """Return the storage-space/key pair for the configured Den endpoint."""
        if self._uses_core_compat_storage():
            return self._core_storage_space(space), self._core_memory_key(space, slug)
        return space, slug

    def _entry_id_for_read(self, space: str, slug: str) -> str:
        """Return the endpoint-specific entry id for a user-facing space/slug."""
        if self._uses_core_compat_storage():
            storage_space, key = self._store_payload_fields(space, slug)
            return self._core_entry_id(storage_space, key)
        return slug

    def _label_result(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Attach source labels so retrieved content is traceable."""
        metadata = raw.get("metadata") or {}
        slug = metadata.get("slug") or raw.get("slug") or raw.get("key") or raw.get("entryId") or raw.get("id", "unknown")
        return {
            "space": metadata.get("space") or metadata.get("intended_space") or raw.get("space", "unknown"),
            "slug": slug,
            "entry_id": raw.get("entryId") or raw.get("entry_id") or raw.get("id") or slug,
            "summary": raw.get("summary") or raw.get("snippet") or metadata.get("summary", ""),
            "content": raw.get("content", ""),
            "metadata": metadata,
            "provenance": raw.get("provenance", {}),
        }

    def _label_results(self, response: dict[str, Any] | list[Any]) -> dict[str, Any]:
        if isinstance(response, list):
            entries = response
        else:
            entries = response.get("entries") or response.get("results") or []
        labeled = [self._label_result(e) for e in entries if isinstance(e, dict)]
        return {
            "status": "ok",
            "results": labeled,
            "count": len(labeled),
        }

    def _filter_core_results_for_spaces(
        self, entries: list[Any], spaces: list[str]
    ) -> list[dict[str, Any]]:
        """Filter live Core search results back to Hermes user-facing spaces."""
        if not self._uses_core_compat_storage():
            return [entry for entry in entries if isinstance(entry, dict)]
        prefixes = {
            self._core_safe(space) + "-": space
            for space in spaces
        }
        filtered: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            metadata = entry.get("metadata")
            if isinstance(metadata, dict) and metadata.get("space") in spaces:
                filtered.append(entry)
                continue
            key = str(entry.get("key") or entry.get("slug") or "")
            for prefix, user_space in prefixes.items():
                if key.startswith(prefix):
                    stripped = key[len(prefix):]
                    patched = dict(entry)
                    patched["key"] = stripped
                    patched["space"] = user_space
                    filtered.append(patched)
                    break
        return filtered

    def _map_error(self, exc: Exception) -> dict[str, Any]:
        """Convert exceptions into structured tool errors."""
        if isinstance(exc, DenUnavailableError):
            return {
                "status": "den_unavailable",
                "error": str(exc),
                "recovery": "Check Den Core health or retry later.",
            }
        if isinstance(exc, urllib.error.HTTPError):
            code = exc.code
            if code == 403:
                return {
                    "status": "permission_denied",
                    "error": f"Den Core returned 403: {exc.reason}",
                    "recovery": "Verify the space is in your configured read_spaces or write_spaces.",
                }
            if code == 404:
                return {
                    "status": "not_found",
                    "error": f"Den Core returned 404: {exc.reason}",
                    "recovery": "Check the slug/space or use den_search to discover entries.",
                }
            if code >= 500:
                return {
                    "status": "den_unavailable",
                    "error": f"Den Core returned {code}: {exc.reason}",
                    "recovery": "Retry later or check Den Core health.",
                }
            return {
                "status": "den_error",
                "error": f"Den Core returned {code}: {exc.reason}",
                "recovery": "Review the request parameters.",
            }
        if isinstance(exc, json.JSONDecodeError):
            return {
                "status": "den_unavailable",
                "error": f"Den Core returned invalid JSON: {exc}",
                "recovery": "The endpoint may not exist yet; check Den Core version.",
            }
        if isinstance(exc, PermissionError):
            return {
                "status": "permission_denied",
                "error": str(exc),
                "recovery": "Use a space from your read_spaces or write_spaces list.",
            }
        if isinstance(exc, MemoryConfigError):
            return {
                "status": "configuration_error",
                "error": str(exc),
                "recovery": "Check memory provider configuration.",
            }
        return {
            "status": "den_unavailable",
            "error": f"Unexpected error contacting Den: {exc}",
            "recovery": "Check network connectivity and Den Core status.",
        }

    def _project_path(self, suffix: str) -> str:
        project = urllib.parse.quote(self.config.project_id, safe="")
        return f"/api/v1/projects/{project}/memory{suffix}"

    # ------------------------------------------------------------------
    # Explicit write tools
    # ------------------------------------------------------------------

    def den_store(
        self,
        title: str,
        content: str,
        space: str | None = None,
        tags: list[str] | None = None,
        summary: str | None = None,
        slug: str | None = None,
    ) -> dict[str, Any]:
        """Create a new Den memory document with provenance metadata.

        If ``slug`` is omitted it is derived from ``title`` using kebab-case.
        """
        try:
            resolved_space = self._resolve_write_space(space)
            resolved_slug = slug or self._slugify(title)
            if not resolved_slug:
                return {
                    "status": "validation_error",
                    "error": "Could not derive a slug from title; provide slug explicitly.",
                    "recovery": "Provide a non-empty slug or a meaningful title.",
                }
            provenance = self._build_provenance()
            storage_space, storage_key = self._store_payload_fields(resolved_space, resolved_slug)
            payload: dict[str, Any] = {
                "key": storage_key,
                "doc_type": "memory",
                "slug": resolved_slug,
                "space": storage_space,
                "title": title,
                "content": content,
                "metadata": {"title": title, "slug": resolved_slug, "space": resolved_space},
                "provenance": provenance,
            }
            if summary is not None:
                payload["summary"] = summary
                payload["metadata"]["summary"] = summary
            if tags is not None:
                payload["tags"] = tags
                payload["metadata"]["tags"] = tags
            _status, data = self._client.request(
                "POST",
                self._project_path("/entries"),
                json_payload=payload,
            )
            return {
                "status": "ok",
                "slug": resolved_slug,
                "space": resolved_space,
                "provenance": provenance,
                "entry_id": data.get("entryId") if isinstance(data, dict) else self._core_entry_id(storage_space, storage_key),
                "entry": self._label_result(data) if isinstance(data, dict) else data,
            }
        except Exception as exc:
            return self._map_error(exc)

    def den_update(
        self,
        slug: str,
        content: str,
        space: str | None = None,
        tags: list[str] | None = None,
        summary: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        """Replace an existing Den memory document, preserving doc_type and injecting fresh provenance."""
        try:
            resolved_space = self._resolve_write_space(space)
            provenance = self._build_provenance()
            storage_space, storage_key = self._store_payload_fields(resolved_space, slug)
            payload: dict[str, Any] = {
                "key": storage_key,
                "doc_type": "memory",
                "slug": slug,
                "space": storage_space,
                "content": content,
                "metadata": {"slug": slug, "space": resolved_space},
                "provenance": provenance,
            }
            if title is not None:
                payload["title"] = title
                payload["metadata"]["title"] = title
            if summary is not None:
                payload["summary"] = summary
                payload["metadata"]["summary"] = summary
            if tags is not None:
                payload["tags"] = tags
                payload["metadata"]["tags"] = tags
            _status, data = self._client.request(
                "POST",
                self._project_path("/entries"),
                json_payload=payload,
            )
            return {
                "status": "ok",
                "slug": slug,
                "space": resolved_space,
                "provenance": provenance,
                "entry_id": data.get("entryId") if isinstance(data, dict) else self._core_entry_id(storage_space, storage_key),
                "entry": self._label_result(data) if isinstance(data, dict) else data,
            }
        except Exception as exc:
            return self._map_error(exc)

    # ------------------------------------------------------------------
    # Explicit read tools
    # ------------------------------------------------------------------

    def den_search(self, query: str, space: str | None = None, limit: int = 10) -> dict[str, Any]:
        """Full-text search across configured read spaces (or an explicit space)."""
        try:
            spaces = self._resolve_spaces(space)
            if not spaces:
                return {
                    "status": "configuration_error",
                    "error": "No read_spaces configured for this profile.",
                    "recovery": "Add spaces to the memory.read_spaces config.",
                }
            search_spaces = ["project"] if self._uses_core_compat_storage() else spaces
            request_limit = max(limit * 10, 50) if self._uses_core_compat_storage() else limit
            payload: dict[str, Any] = {"query": query, "spaces": search_spaces, "limit": request_limit}
            _status, data = self._client.request(
                "POST",
                self._project_path("/search"),
                json_payload=payload,
            )
            entries = data if isinstance(data, list) else (
                data.get("entries") or data.get("results") or []
            )
            return self._label_results(self._filter_core_results_for_spaces(entries, spaces))
        except Exception as exc:
            return self._map_error(exc)

    def den_recall(
        self,
        query: str,
        space: str | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Recall memories by query, optionally filtered by tags."""
        try:
            spaces = self._resolve_spaces(space)
            if not spaces:
                return {
                    "status": "configuration_error",
                    "error": "No read_spaces configured for this profile.",
                    "recovery": "Add spaces to the memory.read_spaces config.",
                }
            search_spaces = ["project"] if self._uses_core_compat_storage() else spaces
            request_limit = max(limit * 10, 50) if self._uses_core_compat_storage() else limit
            payload: dict[str, Any] = {"query": query, "spaces": search_spaces, "limit": request_limit}
            if tags:
                payload["tags"] = tags
            _status, data = self._client.request(
                "POST",
                self._project_path("/search"),
                json_payload=payload,
            )
            entries = data if isinstance(data, list) else (
                data.get("entries") or data.get("results") or []
            )
            entries = self._filter_core_results_for_spaces(entries, spaces)
            # Client-side tag filtering when Den Core does not yet support tags.
            # Some live compatibility search responses return summaries only
            # (entryId/key/space/snippet) without metadata. In that case keep
            # the server-filtered result rather than dropping every match.
            if tags:
                filtered: list[dict[str, Any]] = []
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    metadata = entry.get("metadata")
                    if not isinstance(metadata, dict) or "tags" not in metadata:
                        if self._uses_core_compat_storage():
                            slug = str(entry.get("slug") or entry.get("key") or "")
                            entry_space = str(entry.get("space") or (spaces[0] if spaces else ""))
                            if slug and entry_space:
                                read_result = self.den_read(slug, space=entry_space)
                                if read_result.get("status") == "ok":
                                    full_entry = read_result.get("entry") or {}
                                    full_tags = (full_entry.get("metadata") or {}).get("tags", [])
                                    if any(t in full_tags for t in tags):
                                        filtered.append(full_entry)
                        continue
                    entry_tags = metadata.get("tags", [])
                    if any(t in entry_tags for t in tags):
                        filtered.append(entry)
                entries = filtered
            return self._label_results(entries)
        except Exception as exc:
            return self._map_error(exc)

    def den_read(self, slug: str, space: str | None = None) -> dict[str, Any]:
        """Fetch a single memory entry by slug from configured read spaces."""
        try:
            spaces = self._resolve_spaces(space)
            if not spaces:
                return {
                    "status": "configuration_error",
                    "error": "No read_spaces configured for this profile.",
                    "recovery": "Add spaces to the memory.read_spaces config.",
                }
            not_found_count = 0
            for s in spaces:
                entry_id = self._entry_id_for_read(s, slug)
                path = (
                    self._project_path(f"/entries/{urllib.parse.quote(entry_id, safe='')}")
                    + f"?space={urllib.parse.quote(s, safe='')}&limit=1"
                )
                try:
                    _status, data = self._client.request("GET", path)
                    if isinstance(data, dict) and "space" not in data:
                        data = {**data, "space": s}
                    return {
                        "status": "ok",
                        "entry": self._label_result(data),
                    }
                except urllib.error.HTTPError as exc:
                    if exc.code == 404 and space is None:
                        not_found_count += 1
                        continue
                    raise
            if not_found_count == len(spaces):
                return {
                    "status": "not_found",
                    "error": f"No Den memory entry {slug!r} found in configured read_spaces.",
                    "recovery": "Check the slug/space or use den_search to discover entries.",
                }
            return {
                "status": "not_found",
                "error": f"No Den memory entry {slug!r} found.",
                "recovery": "Check the slug/space or use den_search to discover entries.",
            }
        except Exception as exc:
            return self._map_error(exc)

    def den_list_my_memories(self, space: str | None = None, limit: int = 50) -> dict[str, Any]:
        """List memory entries across read spaces (or a single explicit space)."""
        try:
            spaces = self._resolve_spaces(space)
            if not spaces:
                return {
                    "status": "configuration_error",
                    "error": "No read_spaces configured for this profile.",
                    "recovery": "Add spaces to the memory.read_spaces config.",
                }
            all_entries: list[dict[str, Any]] = []
            errors: list[Exception] = []
            for s in spaces:
                try:
                    if self._uses_core_compat_storage():
                        _status, data = self._client.request(
                            "POST",
                            self._project_path("/search"),
                            json_payload={"query": "", "spaces": ["project"], "limit": max(limit * 10, 50)},
                        )
                    else:
                        path = (
                            self._project_path("/entries")
                            + f"?space={urllib.parse.quote(s, safe='')}&limit={limit}"
                        )
                        _status, data = self._client.request("GET", path)
                    if isinstance(data, list):
                        all_entries.extend(self._filter_core_results_for_spaces(data, [s]))
                    elif isinstance(data, dict):
                        entries = data.get("entries") or data.get("results") or []
                        all_entries.extend(self._filter_core_results_for_spaces(entries, [s]))
                except Exception as exc:
                    # If one space query fails but another succeeds, return the
                    # successful scoped entries. If every space fails, surface a
                    # structured Den error rather than fabricating an empty list.
                    errors.append(exc)
                    continue
            if errors and not all_entries and len(errors) == len(spaces):
                return self._map_error(errors[0])
            return self._label_results(all_entries)
        except Exception as exc:
            return self._map_error(exc)

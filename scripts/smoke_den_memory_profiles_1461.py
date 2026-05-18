#!/usr/bin/env python3
"""Smoke-test the initial opt-in Den memory provider across explicit profiles.

This is intentionally a profile/provider smoke, not a production rollout.  It
uses an in-process Den-memory-compatible HTTP server so the Hermes-side provider
contract can be verified even when the live Den Core memory REST endpoints are
not yet deployed on the current host.  The script also probes the live endpoint
and reports whether the live service is available.
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import re
import socket
import sys
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from den_hermes.memory.config import DenMemoryConfig
from den_hermes.memory.provider import DenMemoryProvider

PROJECT_ID = "den-hermes-bridge"
TASK_ID = 1461


def core_safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower()).strip("-")


def core_entry_id(space: str, key: str) -> str:
    return f"memory-{core_safe(space)}-{core_safe(key)}"


@dataclass(frozen=True)
class SmokeProfile:
    name: str
    read_spaces: tuple[str, ...]
    write_spaces: tuple[str, ...]
    default_write_space: str


GUINEA_PIG_PROFILES = (
    SmokeProfile(
        name="researcher",
        read_spaces=("assistant:researcher",),
        write_spaces=("assistant:researcher",),
        default_write_space="assistant:researcher",
    ),
    SmokeProfile(
        name="reviewer",
        read_spaces=("assistant:reviewer", "knowledge_base:den-memory-smoke"),
        write_spaces=("assistant:reviewer", "knowledge_base:den-memory-smoke"),
        default_write_space="assistant:reviewer",
    ),
    SmokeProfile(
        name="system-architect",
        read_spaces=("assistant:system-architect", "knowledge_base:den-memory-smoke"),
        write_spaces=("assistant:system-architect", "knowledge_base:den-memory-smoke"),
        default_write_space="assistant:system-architect",
    ),
)


class MemoryStore:
    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], dict[str, Any]] = {}

    def upsert(self, payload: dict[str, Any]) -> dict[str, Any]:
        entry = dict(payload)
        if "slug" not in entry and "key" in entry:
            entry["slug"] = entry["key"]
        entry.setdefault("key", entry.get("slug", ""))
        entry.setdefault("entryId", core_entry_id(str(entry.get("space", "project")), str(entry["key"])))
        entry.setdefault("metadata", {})
        entry.setdefault("summary", entry["metadata"].get("summary", ""))
        if "tags" in entry:
            entry.setdefault("metadata", {})["tags"] = entry["tags"]
        entry.setdefault("provenance", payload.get("provenance", {}))
        self.entries[(entry["space"], entry["slug"])] = entry
        return entry

    def list(self, space: str, limit: int) -> list[dict[str, Any]]:
        rows = [v for (s, _), v in self.entries.items() if s == space]
        rows.sort(key=lambda item: item.get("slug", ""))
        return rows[:limit]

    def get(self, space: str, slug: str) -> dict[str, Any] | None:
        if slug.startswith("memory-"):
            for (_entry_space, _), entry in self.entries.items():
                if entry.get("entryId") == slug:
                    return entry
        return self.entries.get((space, slug))

    def search(self, query: str, spaces: list[str], limit: int) -> list[dict[str, Any]]:
        terms = [t.lower() for t in query.split() if t.strip()]
        out: list[dict[str, Any]] = []
        for (space, _), entry in self.entries.items():
            if space not in spaces:
                continue
            haystack = "\n".join(
                str(entry.get(key, "")) for key in ("title", "summary", "content", "slug")
            ).lower()
            if not terms or any(term in haystack for term in terms):
                out.append(entry)
        out.sort(key=lambda item: item.get("slug", ""))
        return out[:limit]


class FakeDenMemoryHandler(http.server.BaseHTTPRequestHandler):
    store: MemoryStore

    def log_message(self, format: str, *args: Any) -> None:  # keep smoke output clean
        del format, args
        return

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _parse_path(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urllib.parse.urlparse(self.path)
        return parsed.path, urllib.parse.parse_qs(parsed.query)

    def do_POST(self) -> None:  # noqa: N802
        path, _query = self._parse_path()
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        prefix = f"/api/v1/projects/{PROJECT_ID}/memory"
        if path == prefix + "/entries":
            return self._json(200, self.store.upsert(payload))
        if path == prefix + "/search":
            return self._json(
                200,
                {
                    "results": self.store.search(
                        str(payload.get("query", "")),
                        list(payload.get("spaces", [])),
                        int(payload.get("limit", 10)),
                    )
                },
            )
        return self._json(404, {"error": "not found"})

    def do_GET(self) -> None:  # noqa: N802
        path, query = self._parse_path()
        prefix = f"/api/v1/projects/{PROJECT_ID}/memory"
        if path == prefix + "/entries":
            space = query.get("space", [""])[0]
            limit = int(query.get("limit", ["50"])[0])
            return self._json(200, {"entries": self.store.list(space, limit)})
        entry_prefix = prefix + "/entries/"
        if path.startswith(entry_prefix):
            slug = urllib.parse.unquote(path[len(entry_prefix):])
            space = query.get("space", [""])[0]
            found = self.store.get(space, slug)
            if found is None:
                return self._json(404, {"error": "not found"})
            return self._json(200, found)
        if path == prefix + "/spaces":
            spaces = sorted({space for space, _slug in self.store.entries})
            return self._json(200, {"spaces": spaces})
        return self._json(404, {"error": "not found"})


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_fake_server() -> tuple[http.server.ThreadingHTTPServer, str]:
    store = MemoryStore()
    handler = type("BoundFakeDenMemoryHandler", (FakeDenMemoryHandler,), {"store": store})
    port = free_port()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}"


def provider(profile: SmokeProfile, base_url: str) -> DenMemoryProvider:
    cfg = DenMemoryConfig(
        base_url=base_url,
        project_id=PROJECT_ID,
        requested_by=profile.name,
        run_id="task1461-smoke",
        role="non_worker_smoke",
        task_id=TASK_ID,
    )
    return DenMemoryProvider(
        config=cfg,
        read_spaces=profile.read_spaces,
        default_space=profile.read_spaces[0],
        write_spaces=profile.write_spaces,
        default_write_space=profile.default_write_space,
    )


def file_digest(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def profile_memory_files(profile_root: Path, names: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in names:
        root = profile_root / name
        memory_file = root / "memories" / "MEMORY.md"
        user_file = root / "memories" / "USER.md"
        result[name] = {
            "profile_dir_exists": root.is_dir(),
            "MEMORY.md": file_digest(memory_file),
            "USER.md": file_digest(user_file),
            "MEMORY.md_path": str(memory_file),
            "USER.md_path": str(user_file),
        }
    return result


def assert_ok(label: str, result: dict[str, Any]) -> None:
    if result.get("status") != "ok":
        raise AssertionError(f"{label} expected ok, got {result}")


def run_contract_smoke(base_url: str) -> dict[str, Any]:
    profiles = {p.name: provider(p, base_url) for p in GUINEA_PIG_PROFILES}

    solo_store = profiles["researcher"].den_store(
        "Task 1461 solo assistant memory",
        "The solo researcher profile keeps assistant-space memories isolated from the shared KB.",
        slug="task1461-solo-assistant-memory",
        tags=["task-1461", "solo"],
        summary="Solo assistant-space memory for #1461 smoke.",
    )
    assert_ok("solo_store", solo_store)

    shared_store = profiles["reviewer"].den_store(
        "Task 1461 shared KB memory",
        "Shared KB marker: cross-profile Den memory recall works for reviewer and system-architect.",
        space="knowledge_base:den-memory-smoke",
        slug="task1461-shared-kb-memory",
        tags=["task-1461", "shared-kb"],
        summary="Shared KB memory for #1461 smoke.",
    )
    assert_ok("shared_store", shared_store)

    cross_recall = profiles["system-architect"].den_recall(
        "cross-profile recall", space="knowledge_base:den-memory-smoke", tags=["shared-kb"]
    )
    assert_ok("cross_recall", cross_recall)
    assert cross_recall["count"] == 1, cross_recall
    assert cross_recall["results"][0]["slug"] == "task1461-shared-kb-memory", cross_recall

    direct_read = profiles["system-architect"].den_read(
        "task1461-shared-kb-memory", space="knowledge_base:den-memory-smoke"
    )
    assert_ok("direct_read", direct_read)

    solo_cannot_search_shared = profiles["researcher"].den_recall(
        "shared", space="knowledge_base:den-memory-smoke"
    )
    assert solo_cannot_search_shared["status"] == "permission_denied", solo_cannot_search_shared

    list_reviewer = profiles["reviewer"].den_list_my_memories(space="knowledge_base:den-memory-smoke")
    assert_ok("list_reviewer", list_reviewer)
    assert list_reviewer["count"] == 1, list_reviewer

    unavailable = provider(GUINEA_PIG_PROFILES[0], "http://127.0.0.1:9").den_recall("anything")
    assert unavailable["status"] == "den_unavailable", unavailable

    return {
        "solo_store": solo_store,
        "shared_store": shared_store,
        "cross_recall_count": cross_recall["count"],
        "direct_read_slug": direct_read["entry"]["slug"],
        "solo_shared_space_denied": solo_cannot_search_shared["status"],
        "list_reviewer_count": list_reviewer["count"],
        "den_unavailable_status": unavailable["status"],
    }


def probe_live_endpoint(url: str) -> dict[str, Any]:
    live = provider(GUINEA_PIG_PROFILES[0], url)
    result = live.den_list_my_memories(space="assistant:researcher", limit=1)
    return {
        "base_url": url,
        "status": result.get("status"),
        "error": result.get("error", ""),
        "recovery": result.get("recovery", ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile-root", default="/home/agents/profiles")
    parser.add_argument("--live-base-url", default="http://192.168.1.10:5299")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    server, fake_base_url = start_fake_server()
    try:
        contract = run_contract_smoke(fake_base_url)
    finally:
        server.shutdown()
        server.server_close()

    profile_names = [p.name for p in GUINEA_PIG_PROFILES]
    memory_files = profile_memory_files(Path(args.profile_root), profile_names)
    memory_paths = {
        details["MEMORY.md_path"]
        for details in memory_files.values()
        if details["MEMORY.md"] is not None
    }
    user_paths = {
        details["USER.md_path"]
        for details in memory_files.values()
        if details["USER.md"] is not None
    }
    memory_file_count = sum(1 for d in memory_files.values() if d["MEMORY.md"] is not None)
    user_file_count = sum(1 for d in memory_files.values() if d["USER.md"] is not None)
    existing_memory_count = sum(
        1
        for details in memory_files.values()
        if details["MEMORY.md"] is not None or details["USER.md"] is not None
    )
    distinct_paths = len(memory_paths) == memory_file_count and len(user_paths) == user_file_count

    payload = {
        "status": "passed",
        "task_id": TASK_ID,
        "guinea_pig_profiles": [p.__dict__ for p in GUINEA_PIG_PROFILES],
        "contract_smoke": contract,
        "profile_memory_files": memory_files,
        "profile_memory_paths_distinct": distinct_paths,
        "profile_memory_files_existing_count": existing_memory_count,
        "live_endpoint_probe": probe_live_endpoint(args.live_base_url),
        "notes": [
            "Hermes-side provider contract passed with an in-process Den-memory-compatible HTTP server.",
            "Live endpoint probe is reported separately; den_unavailable is expected until Den Core exposes /api/v1/projects/{project}/memory endpoints on the configured base URL.",
        ],
    }
    print(json.dumps(payload, indent=2, sort_keys=True) if args.json else payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

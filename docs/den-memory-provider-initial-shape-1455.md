# Den-backed long-term memory provider — initial shape

Task: `den-hermes-bridge` #1455  
Parent: #1453 (spaces config)  
Deferred advanced super: #1454  
Implementation child tasks: #1457 (read tools), #1459 (write tools), #1460 (provider wiring)  
Status: design/doc; no implementation of read/write tools in this task  

## 1. Purpose and constraints

This document specifies the initial opt-in Den-backed long-term memory provider for Hermes. It is strictly manual-tools-only in this increment: the model must explicitly choose to read or write memory entries via surfaced tools. There is no automatic prefetch, no automatic compression, no automatic turn sync, and no implicit delegation memory capture.

Key constraints:
- **No automatic memory behavior** in the initial super.
- **No Den MCP transport** for provider machinery; the provider calls Den Core REST directly.
- The provider lives in the bridge repo/plugin layer, not in Hermes core.
- All memory entries carry provenance (run, role, task, timestamp).
- Den unavailability must degrade gracefully rather than blocking the worker.

## 2. Provider placement

Module path (proposed):

```text
den_hermes/memory/
  __init__.py
  provider.py          # DenMemoryProvider class + hook implementations
  rest_client.py       # Thin Den Core REST client for memory endpoints
  tools.py             # Model-callable tool definitions (manual read/write/search)
  config.py            # Pydantic/dataclass config schema
  provenance.py        # Provenance builder and validation
  errors.py            # Provider-level exceptions
```

The provider is a bridge-owned plugin. It is instantiated by the bridge launcher or orchestrator and injected into the worker context. Hermes core does not need to know about Den memory semantics; it sees only the standard tool surface that the provider registers.

The provider must be opt-in per role via the runtime registry:

```yaml
roles:
  coder:
    runtime_id: coder-primary
    # ... existing fields ...
    memory:
      enabled: true
      spaces:
        - project
        - task
      default_scope: task
      deny_auto_behavior: true   # must be true for this initial super
```

If `memory.enabled` is absent or `false`, the provider is not instantiated and no memory tools are registered.

## 3. MemoryProvider hook enumeration

The following hook surface is the contract between the bridge orchestrator/supervisor and the memory provider. Each hook is marked as either **implemented** (manual-tools-only behavior) or **no-op/deferred** to the advanced super (#1454).

| Hook | Status | Notes |
|------|--------|-------|
| `prefetch` | **no-op / deferred** | Automatic pre-turn memory retrieval is forbidden in the initial super. The model may call explicit read/search tools instead. |
| `queue_prefetch` | **no-op / deferred** | Background/async prefetch queue is deferred to #1454. |
| `sync_turn` | **no-op / deferred** | Automatic per-turn memory synchronization (e.g., flushing working memory to Den) is deferred to #1454. |
| `on_session_end` | **no-op / deferred** | Automatic session-end memory write (e.g., summarization, compression) is deferred to #1454. |
| `on_pre_compress` | **no-op / deferred** | Automatic compression-triggered memory capture is deferred to #1454. |
| `on_memory_write` | **no-op / deferred** | Automatic interception of all memory writes for indexing/deduplication is deferred to #1454. |
| `on_delegation` | **no-op / deferred** | Automatic capture of delegation context into Den memory is deferred to #1454. |
| `read` | **implemented** | Explicit tool: `den_memory_read`. Retrieves an entry by key/ID. |
| `search` | **implemented** | Explicit tool: `den_memory_search`. Queries entries by text/vector/metadata. |
| `write` | **implemented** | Explicit tool: `den_memory_write`. Stores an entry with required provenance. |
| `delete` | **implemented** | Explicit tool: `den_memory_delete`. Soft-deletes an entry by key/ID (marks tombstoned). |
| `list_spaces` | **implemented** | Explicit tool: `den_memory_list_spaces`. Returns visible spaces for the current task/project. |

Implementation rule: every no-op hook must log at `debug` level when called so that future supervisors can observe the call site without producing side effects. They must not raise.

## 4. Transport target: Den Core REST

The provider must target Den Core REST endpoints, not the `den-mcp` MCP adapter.

Base URL resolution order:
1. `DEN_CORE_REST_URL` environment variable (e.g., `http://192.168.1.10:5000`)
2. `den_core_rest_url` in the runtime registry role memory config
3. Default: `http://192.168.1.10:5000` (documented, not hard-coded as production default)

Required headers for every request:
- `Content-Type: application/json`
- `Accept: application/json`
- `X-Den-Project-Id: <project_id>`
- `X-Den-Requested-By: <requested_by>`
- `X-Den-Run-Id: <run_id>`
- `X-Den-Role: <role>`

Authentication: reuse the same bearer token mechanism that the MCP adapter uses, but resolved independently via `DEN_CORE_API_TOKEN` or a bridge-local credential file (path configurable, never stored in Den metadata).

Target endpoints (proposed, subject to Den Core API availability):

| Operation | Method | Path | Body / Query |
|-----------|--------|------|--------------|
| Read entry | `GET` | `/api/v1/projects/{project_id}/memory/entries/{entry_id}` | — |
| Search entries | `POST` | `/api/v1/projects/{project_id}/memory/search` | `{ "query": "...", "spaces": [...], "limit": 10 }` |
| Write entry | `POST` | `/api/v1/projects/{project_id}/memory/entries` | `{ "key": "...", "space": "...", "content": "...", "provenance": {...}, "metadata": {...} }` |
| Delete entry | `DELETE` | `/api/v1/projects/{project_id}/memory/entries/{entry_id}` | `{ "tombstone_reason": "...", "provenance": {...} }` |
| List spaces | `GET` | `/api/v1/projects/{project_id}/memory/spaces` | — |

If Den Core does not yet expose these exact paths, the provider must surface a clear `DenCoreApiGapError` and degrade gracefully (see Section 8).

## 5. Tool surface (model-callable)

These tools are registered into the Hermes worker toolset when `memory.enabled: true`.

### `den_memory_read`

```json
{
  "entry_id": "string (required)",
  "space": "string (optional; narrows lookup)"
}
```

Returns the entry content, provenance, and metadata. If the entry is tombstoned, returns `status: deleted` and the tombstone reason.

### `den_memory_search`

```json
{
  "query": "string (required)",
  "spaces": ["string"],
  "limit": "integer, default 10, max 100",
  "task_id": "integer (optional; if omitted, uses current task)",
  "include_tombstoned": "boolean, default false"
}
```

Returns a ranked list of matching entries. The provider must scope the search to the union of:
- the spaces listed in the tool argument,
- the spaces configured in the runtime registry for the current role,
- plus any spaces the current task/thread explicitly shares.

### `den_memory_write`

```json
{
  "key": "string (required; unique within space)",
  "space": "string (required; must be in allowed spaces list)",
  "content": "string (required; max 128 KiB in this super)",
  "metadata": "object (optional; arbitrary JSON, max 16 KiB)"
}
```

Writes the entry to Den. The provider auto-injects provenance; the model must not supply `provenance` directly. If the key already exists in the space, the behavior is `upsert` (overwrite with new version and provenance chain), not reject.

### `den_memory_delete`

```json
{
  "entry_id": "string (required)",
  "reason": "string (optional; stored as tombstone_reason)"
}
```

Soft-deletes the entry. Hard delete is deferred to a future admin tool.

### `den_memory_list_spaces`

```json
{
  "project_id": "string (optional; default current project)"
}
```

Returns the list of spaces visible to the current role/run, derived from runtime registry config plus any Den-shared spaces.

Tool naming rule: all tools are prefixed `den_memory_` to avoid collision with generic `memory_*` tools that may exist in Hermes core.

## 6. Config schema

```yaml
# Inline in spawned-hermes-runtimes.sample.yaml under role.memory
memory:
  enabled: bool                       # default false
  spaces: list[str]                   # e.g. [project, task, session, global]
  default_scope: str                  # fallback space when the model omits space
  deny_auto_behavior: bool            # must be true; provider refuses to instantiate if false
  rest:
    base_url: str | null              # overrides env var
    timeout_seconds: int              # default 30
    retry_attempts: int               # default 2
    credential_path: str | null       # path to bearer token file; default null -> env var
  provenance:
    include_full_prompt_hash: bool    # default false; if true, hashes the prompt packet
    include_git_head: bool            # default true; includes repo head commit in provenance
  content_limits:
    max_entry_bytes: int              # default 131072 (128 KiB)
    max_metadata_bytes: int           # default 16384 (16 KiB)
    max_search_results: int           # default 100
```

Validation rules:
- If `enabled: true` and `deny_auto_behavior: false`, instantiation must raise `MemoryConfigError` with a message explaining that automatic behavior is deferred to #1454.
- `spaces` must be non-empty when enabled.
- `default_scope` must be an element of `spaces`.

## 7. System prompt block

When the provider is enabled, the bridge appends a bounded memory system prompt block to the worker startup prompt. It must not exceed 800 tokens and must not contain full memory contents.

Template:

```text
[Den memory]
You have access to Den-backed long-term memory via explicit tools:
- den_memory_read(entry_id)
- den_memory_search(query, spaces, limit)
- den_memory_write(key, space, content, metadata)
- den_memory_delete(entry_id, reason)
- den_memory_list_spaces()

Allowed spaces for this run: {{ spaces | join(", ") }}
Default space when omitted: {{ default_scope }}
Max entry size: {{ max_entry_bytes }} bytes
Max metadata size: {{ max_metadata_bytes }} bytes
Provenance tracking is enabled. Every write records run_id, role, task_id, and timestamp.
Automatic memory behavior (prefetch, sync, compression capture, delegation capture) is DISABLED in this super. Use explicit tool calls only.
```

The block is injected by the bridge launcher before the bounded Den context packet, not appended by the model.

## 8. Den unavailable behavior

The provider must degrade gracefully when Den Core REST is unreachable or returns errors.

| Scenario | Behavior |
|----------|----------|
| Den REST unreachable at startup | Log warning; instantiate provider in `offline` mode; tools return `den_unavailable` errors with recovery guidance. |
| Den REST fails during read/search | Return tool error with `status: den_unavailable`; do not crash the worker turn. |
| Den REST fails during write | Return tool error with `status: den_unavailable`; optionally buffer to a local JSONL spill file for later replay (buffer limit: 100 entries, 10 MiB). |
| Den REST returns 404 for an entry | Return `not_found` to the model; this is normal, not an error. |
| Den REST returns 403 | Return `permission_denied`; model should try a different space or task scope. |
| Den REST returns 5xx | Retry with exponential backoff (max 3 attempts, base delay 1s); if all fail, return `den_unavailable`. |

Offline mode rules:
- `den_memory_read` and `den_memory_search` return `den_unavailable` immediately.
- `den_memory_write` appends to the local spill file if under limits; otherwise returns `den_unavailable`.
- `den_memory_delete` returns `den_unavailable`.
- `den_memory_list_spaces` returns the statically configured spaces from the runtime registry.

The spill file path: `/tmp/den-hermes/<run_id>/memory_spill.jsonl`. The bridge cleanup logic must include this path in idempotent cleanup.

## 9. Provenance

Every memory entry written through the provider carries a provenance blob:

```json
{
  "run_id": "spawned-hermes-coder-abc123",
  "role": "coder",
  "task_id": 1455,
  "project_id": "den-hermes-bridge",
  "requested_by": "den-hermes-runner",
  "written_at": "2026-05-15T20:51:00Z",
  "hermes_profile": "den-hermes-runner",
  "git_head": "9b36ee029fef2718555cd9761bfcf4f2e146e52f",
  "prompt_packet_message_id": 6061
}
```

Fields:
- `run_id`, `role`, `task_id`, `project_id`, `requested_by`: from the bridge launch context.
- `written_at`: ISO-8601 UTC timestamp.
- `hermes_profile`: from runtime registry.
- `git_head`: optional; collected at startup if `include_git_head: true` and cwd is a git repo.
- `prompt_packet_message_id`: optional; the Den task-thread packet message id that bounded this run.

The provenance blob is immutable after write. Upserts append a new `version_provenance` array rather than overwriting the original provenance.

## 10. Spaces config decisions from #1453 / #6040 / #6057

The following decisions are incorporated so that implementation subtasks can proceed without re-reading parent thread internals.

### 10.1 Space taxonomy

Spaces are coarse namespaces within Den memory. The initial super supports exactly these five space kinds:

| Space | Visibility | Lifetime | Use case |
|-------|-----------|----------|----------|
| `project` | Project-wide | Indefinite | Cross-task conventions, API contracts, rollout guidance. |
| `task` | Task-scoped | Task lifetime | Task-specific design notes, intermediate findings, acceptance criteria. |
| `session` | Run-scoped | Run lifetime | Ephemeral working notes for the current spawned-Hermes run. |
| `global` | Project-wide | Indefinite | Rare; only for bridge-wide shared memory. |
| `review` | Review-round-scoped | Review round lifetime | Review findings, verdict rationale, retry notes. |

### 10.2 Space selection rules

- The model may specify any space in `den_memory_write(space=...)`.
- The provider validates that the requested space is in the role's configured `spaces` list.
- If the model omits the space, the provider uses `default_scope`.
- `den_memory_search` without explicit spaces searches the union of all configured spaces for the role.
- The `review` space is automatically appended when the current task has an active review round, even if not in the role config.

### 10.3 Space isolation guarantees

- Keys are unique within a `(project_id, space)` pair, not globally unique.
- Different tasks may share the `project` space; collisions are resolved as last-write-wins with provenance.
- The `task` space is scoped to `task_id`; queries to `task` space are implicitly filtered by the current task unless the tool explicitly overrides `task_id`.

### 10.4 Config migration path

When #1453 introduced spaces, the default config for all existing roles was empty. New roles should explicitly opt into memory. Existing roles will not get memory tools until an operator updates the runtime registry.

## 11. Den Core API gaps and follow-up candidates

The provider design assumes Den Core endpoints that may not yet exist. The following gaps must be treated as explicit follow-up candidates, not hidden assumptions.

| Gap | Impact | Proposed workaround in provider |
|-----|--------|--------------------------------|
| No `POST /memory/search` endpoint | Search tool cannot function. | Fallback to `GET /memory/entries?space=...&limit=...` and client-side text filtering; if neither exists, return `den_unavailable` with a note. |
| No `DELETE /memory/entries/{id}` soft-delete | Delete tool cannot function. | Return `den_unavailable` and log the gap; do not attempt hard delete. |
| No provenance field in entry schema | Provenance must be stored inside `metadata`. | Store provenance under `_provenance` key in `metadata`; add a migration note to move it to a top-level field when Core supports it. |
| No `task_id` scoping in memory endpoints | Task space isolation is client-side only. | Provider prefixes task-scoped keys with `task_{task_id}_` as a soft-namespace convention; document this as a temporary measure. |
| No vector/rank search | Search is text-only. | Accept text-only search and document the limitation; do not implement client-side vector search. |
| No memory-specific rate limits documented | Provider may hit generic rate limits. | Implement the same retry/backoff used for other Den REST calls; document that memory endpoints should have their own limits in Core. |
| No bulk read/write endpoints | Large batch operations are inefficient. | Enforce per-entry limits and small batch sizes; defer bulk operations to a future advanced super. |

Follow-up tasks should be created for each gap once Den Core confirms availability or rejects the endpoint shape.

## 12. Implementation roadmap for child tasks

| Task | Scope | Acceptance criteria derived from this doc |
|------|-------|------------------------------------------|
| #1457 | Read tools (`den_memory_read`, `den_memory_search`, `den_memory_list_spaces`) | REST client can read and search; tools are registered when memory enabled; tests cover 404, 403, 5xx, offline mode. |
| #1459 | Write tools (`den_memory_write`, `den_memory_delete`) | REST client can write and soft-delete; provenance is auto-injected; tests cover size limits, upsert, Den unavailable, spill file. |
| #1460 | Provider wiring and system prompt block | `DenMemoryProvider` instantiates from runtime registry; hooks are no-op/deferred as specified; system prompt block is injected; config validation rejects `deny_auto_behavior: false`. |

## 13. Validation checklist for this design doc

- [ ] All seven automatic-behavior hooks are explicitly listed as no-op/deferred.
- [ ] Transport is Den Core REST, not MCP.
- [ ] Config schema includes `enabled`, `spaces`, `default_scope`, `deny_auto_behavior`.
- [ ] System prompt block template is present and bounded.
- [ ] Den unavailable behavior is specified for reads, writes, deletes, and startup.
- [ ] Provenance schema is specified with all required fields.
- [ ] Spaces taxonomy and isolation rules from #1453 are incorporated.
- [ ] Den Core API gaps are enumerated as explicit follow-up candidates.
- [ ] No secrets are required in files or config (credential is env-var or path-based).
- [ ] No implementation of read/write tools is present in this task.

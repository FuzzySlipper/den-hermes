# Den memory spaces and conventions for Hermes (Spaces-aware)

Task: `den-hermes-bridge` #1456
Parent: #1455 (Den memory provider initial shape)
Status: conventions doc; no provider implementation in this task

## 1. Purpose

This document defines the conventions for allocating, naming, and writing to Den memory spaces when Hermes profiles use the Den-backed memory provider. It is a conventions layer on top of the provider shape defined in #1455; it does not change the provider API.

All conventions here are opt-in and intentional. There is no implicit assistant space per profile, no auto-discovery of shared spaces, and no automatic capture.

## 2. Space taxonomy

Den memory spaces are coarse namespaces. For Hermes, the following three space kinds are the primary surface:

| Space | Visibility | Lifetime | Use case |
|-------|-----------|----------|----------|
| `assistant` | Profile-scoped | Indefinite | Private working memory for a single Hermes profile. Not shared with other profiles. |
| `knowledge_base` | Shared | Indefinite | Curated, durable reference material explicitly shared among a cluster of profiles. |
| `project` | Project-wide | Indefinite | Cross-task conventions, architecture decisions, rollout guidance, and project docs. |

These are distinct from the provider-level spaces `task`, `session`, `review`, and `global` defined in #1455. A profile's runtime config maps Den memory spaces into the profile's visible world. The provider enforces access; this document governs intent.

## 3. Spaces are allocated intentionally

No profile gets an implicit `assistant` space. No profile auto-discovers `knowledge_base` spaces. Every space in a profile's config is explicit.

Implications:
- A profile without memory config has no memory tools.
- A profile with `enabled: true` but an empty spaces list has memory tools but no writable spaces; writes fail with `permission_denied`.
- Shared `knowledge_base` spaces must be listed explicitly in every participating profile's config.

## 4. Profile config surface

The full memory config for a profile in the runtime registry:

```yaml
roles:
  planner:
    memory:
      enabled: true
      default_write_space: assistant
      read_spaces:
        - assistant
        - knowledge_base
      shared_write_spaces:
        - knowledge_base
```

Fields:
- `enabled: bool` — must be `true` for memory tools to be registered.
- `default_write_space: str | null` — the space written to when the model omits `space=...`. Must be an element of `read_spaces`. May be `null` to force the model to always specify a space explicitly.
- `read_spaces: list[str]` — all spaces the profile may read from. Searched by default when `den_memory_search` is called without explicit spaces.
- `shared_write_spaces: list[str] | null` — subset of `read_spaces` that the profile may write to in addition to `default_write_space`. If absent or empty, the profile may write only to `default_write_space`.

Validation rules:
- `default_write_space` must be in `read_spaces` if non-null.
- `shared_write_spaces` must be a subset of `read_spaces`.
- A write request to a space not in `default_write_space | shared_write_spaces` returns `permission_denied`.

## 5. How a profile decides where to write

Decision order:
1. **Explicit `space=` override**: The model passes `space="knowledge_base"` to `den_memory_write`. The provider checks that `knowledge_base` is in `default_write_space | shared_write_spaces`.
2. **Default write space**: If the model omits `space=`, the provider uses `default_write_space`. If `default_write_space` is null, the tool returns an error requiring explicit selection.
3. **No automatic cross-space promotion**: The provider never promotes a write from `assistant` to `knowledge_base` or from `task` to `project`. If the model writes to the wrong space, it must delete and rewrite.

Example: A coder profile with `default_write_space: assistant` and `shared_write_spaces: [knowledge_base]` writes a private workaround note to `assistant` by omitting `space=`. To publish a curated pattern to the team, it explicitly passes `space="knowledge_base"`.

## 6. Concrete cluster examples

### 6.1 Solo-assistant profile

```yaml
roles:
  router:
    memory:
      enabled: true
      default_write_space: assistant
      read_spaces:
        - assistant
```

- Has a private `assistant` space.
- Cannot read or write any shared space.
- Suitable for a singleton router that keeps personal routing heuristics.

### 6.2 Cluster member with own assistant space plus shared kb space

```yaml
roles:
  coder-primary:
    memory:
      enabled: true
      default_write_space: assistant
      read_spaces:
        - assistant
        - knowledge_base
      shared_write_spaces:
        - knowledge_base
```

- Keeps private scratch notes in `assistant`.
- Reads and writes curated patterns to `knowledge_base`.
- Other coders in the cluster with the same `knowledge_base` in their `read_spaces` see the same entries.

### 6.3 Profile participating in multiple shared concerns

```yaml
roles:
  orchestrator:
    memory:
      enabled: true
      default_write_space: assistant
      read_spaces:
        - assistant
        - knowledge_base
        - project
      shared_write_spaces:
        - knowledge_base
        - project
```

- Private orchestration heuristics go to `assistant`.
- Curated runbooks go to `knowledge_base`.
- Cross-task conventions and project-level decisions go to `project`.
- The orchestrator must explicitly choose the space per write; there is no automatic routing.

## 7. Naming conventions

### 7.1 Keys (slugs)
- Use `kebab-case` for keys.
- Prefix with a short topic when useful: `api-pattern-retry-backoff`, `deploy-checklist-v2`.
- Keys are unique within a space. Collisions are last-write-wins with provenance.
- Avoid timestamps in keys; use entry `written_at` or metadata `version` for history.

### 7.2 Title and summary
- Store a human title in metadata: `{"title": "Retry with exponential backoff"}`.
- Store a one-line summary in metadata: `{"summary": "Use 1s base delay, max 3 retries, jitter 0-200ms."}`.
- Titles should be stable across updates so that searches return consistent results.

### 7.3 Tags
- Light free-form tags in metadata: `{"tags": ["api", "reliability", "v1.4"]}`.
- Prefer flat tags over hierarchies; avoid deep nesting.
- Do not use tags as a substitute for space selection. A tag does not change isolation.

## 8. What belongs where: boundaries and examples

### 8.1 Hermes `MEMORY.md`
- **What**: Static, version-controlled markdown that describes a single Hermes profile's long-term behavior, preferences, and constraints.
- **Where**: In the Hermes profile repo, e.g. `profiles/coder/MEMORY.md`.
- **Owner**: The profile maintainer (human or automated PR).
- **Example**: "This coder prefers type hints, avoids `requests` in favor of `httpx`, and formats docstrings in Google style."
- **Not for**: Ephemeral task notes, cross-profile shared knowledge, or auto-captured conversation fragments.

### 8.2 Den project docs
- **What**: Canonical project documentation stored in Den (not in git), often with `doc_type=note` or `doc_type=reference`.
- **Where**: Den project space, surfaced via Den UI or API.
- **Owner**: Project operators and planners.
- **Example**: "Rollout plan for v2.3", "Architecture decision record: switch from SQLite to PostgreSQL".
- **Not for**: Private heuristics, auto-captured chat fragments, or profile-specific preferences.

### 8.3 Den memory docs
- **What**: Short-lived or profile-scoped working memory written through the Den memory provider.
- **Where**: Den memory spaces (`assistant`, `knowledge_base`, `project`, `task`, `session`, `review`).
- **Owner**: The profile that wrote the entry (with provenance).
- **Example**: "In task #1455, the Den Core search endpoint was missing; fallback to client-side filtering worked." (written to `assistant` or `task`)
- **Not for**: Canonical project documentation, version-controlled specs, or large binary artifacts.

### 8.4 Topic-clipping (deferred to advanced super)
- **What**: Automatic extraction of topics or summaries from conversation turns.
- **Where**: Not defined in this super; deferred to #1454.
- **Rule**: No automatic capture in the initial super. Profiles may manually write clips to `assistant` or `task` if they choose.

### 8.5 Comparison matrix

| Content type | Belongs in | Rationale |
|-------------|-----------|-----------|
| Profile preferences | `MEMORY.md` | Version-controlled, human-editable, loaded at profile startup. |
| Cross-task conventions | Den `project` space | Living document, updated by planners, visible to all roles. |
| Curated team patterns | Den `knowledge_base` space | Shared, searchable, maintained explicitly by cluster members. |
| Private heuristics | Den `assistant` space | Profile-scoped, not visible to others, opt-in per profile. |
| Task-specific findings | Den `task` space | Scoped to task lifetime, auto-filtered by task_id. |
| Ephemeral working notes | Den `session` space | Run-scoped, discarded after the run unless promoted. |
| Review findings | Den `review` space | Scoped to review round, auto-appended when active. |

## 9. Project-space drift guidance

Writing `doc_type=memory` into project spaces should be rare. The project space is for canonical project documentation, not for personal working memory.

Prefer these doc types in project space:
- `note` — a project-level observation or decision record.
- `reference` — an API contract, rollout checklist, or onboarding guide.
- `adr` — architecture decision record.

Reserve `doc_type=memory` for:
- `assistant` space (private heuristics).
- `knowledge_base` space (curated patterns that originated as memory but were promoted).
- `task` or `session` space (ephemeral working memory).

If a profile repeatedly writes `doc_type=memory` to `project`, the operator should review whether `default_write_space` is misconfigured or whether the content should live in `knowledge_base` instead.

## 10. Non-worker role examples

Worker profiles (e.g., `coder`, `reviewer`) have no memory by default in the initial super. Non-worker roles may opt in.

### 10.1 Planner
```yaml
roles:
  planner:
    memory:
      enabled: true
      default_write_space: assistant
      read_spaces:
        - assistant
        - project
        - knowledge_base
      shared_write_spaces:
        - project
        - knowledge_base
```
- Writes private planning heuristics to `assistant`.
- Publishes cross-task conventions to `project`.
- Maintains curated runbooks in `knowledge_base`.

### 10.2 Runner
```yaml
roles:
  runner:
    memory:
      enabled: true
      default_write_space: assistant
      read_spaces:
        - assistant
        - knowledge_base
      shared_write_spaces: []
```
- Keeps private execution notes in `assistant`.
- Reads `knowledge_base` for runbooks but does not write to shared spaces.

### 10.3 Router
```yaml
roles:
  router:
    memory:
      enabled: true
      default_write_space: assistant
      read_spaces:
        - assistant
      shared_write_spaces: []
```
- Private routing history and heuristics only.
- No shared write access; routing decisions are ephemeral.

### 10.4 Coder (non-worker variant)
```yaml
roles:
  coder-architect:
    memory:
      enabled: true
      default_write_space: assistant
      read_spaces:
        - assistant
        - knowledge_base
        - project
      shared_write_spaces:
        - knowledge_base
```
- A coding role that is not a tracked spawned worker (e.g., a long-lived assistant instance).
- Writes patterns to `knowledge_base`, keeps private notes in `assistant`.

### 10.5 Orchestrator
```yaml
roles:
  orchestrator:
    memory:
      enabled: true
      default_write_space: assistant
      read_spaces:
        - assistant
        - project
        - knowledge_base
      shared_write_spaces:
        - project
        - knowledge_base
```
- Coordinates across tasks; publishes conventions to `project` and runbooks to `knowledge_base`.
- Private orchestration state stays in `assistant`.

## 11. Worker profiles have no memory

Tracked spawned-Hermes worker profiles (e.g., `coder`, `reviewer`) run bounded tasks and terminate. They do not carry persistent memory between runs. Their context comes from:
- The Den task-thread packet.
- The bounded system prompt.
- Explicit tool calls during the run.

A worker profile may still have `memory.enabled: true` for the duration of a single run, but it should not be treated as a long-term memory holder. Any writes made by a worker are stored in Den with provenance and are visible to future workers if they share the space, but the worker itself does not retain state.

## 12. Validation checklist

- [ ] Convention doc exists and covers `assistant`, `knowledge_base`, and `project` spaces.
- [ ] Spaces allocation is explicitly configured; no implicit or auto-discovered spaces.
- [ ] Profile config surface specifies `enabled`, `default_write_space`, `read_spaces`, and optional `shared_write_spaces`.
- [ ] Write decision rules cover explicit override, default space, and no cross-space promotion.
- [ ] Naming conventions for slugs, titles, summaries, and tags are documented.
- [ ] Boundaries between `MEMORY.md`, Den project docs, Den memory docs, and topic-clipping are clear.
- [ ] Project-space drift guidance warns against `doc_type=memory` in project spaces.
- [ ] Non-worker role examples provided for planner, runner, router, coder, orchestrator.
- [ ] Worker profiles are noted as having no persistent memory.
- [ ] No reliance on automatic capture or advanced super features.

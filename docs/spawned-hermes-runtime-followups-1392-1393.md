# Spawned-Hermes runtime follow-ups (#1392/#1393)

## Scope

This note records the implementation and API decisions from the #1392/#1393 dogfood follow-ups after the central spawned-Hermes runtime registry work.

## Central runtime location

The active operator-owned runtime registry now lives at:

```text
/home/agents/runtime/spawned-hermes-runtimes.yaml
```

Rationale:

- The registry is machine/runtime policy, not a single profile's private state.
- `/home/agents/runtime` is clearer for shared runner operations.
- The directory can be group-owned by `agents` with the setgid bit, avoiding profile-local permission surprises as additional profiles/agents need to read the same runtime picks.

Compatibility:

- `DEN_HERMES_RUNTIME_REGISTRY` still overrides the default for tests and emergency/manual use.
- The previous profile-local file path can remain as a symlink to the central file during rollout:

```text
/home/agents/profiles/den-hermes-runner/runtime/spawned-hermes-runtimes.yaml -> /home/agents/runtime/spawned-hermes-runtimes.yaml
```

## #1392 audit metadata decision

Current bridge behavior:

- The resolver computes sanitized audit metadata in `ResolvedRuntime.audit_metadata()`:
  - `registry_id`
  - `registry_fingerprint`
  - `runtime_id`
  - canonical `role`
  - `resolved_at`
  - explicit override metadata when present
- The local workflow registration path passes primitive resolved launch fields (`profile`, `provider`, `model`, `toolsets`, `timeout_seconds`, paths) and the local `runtime_id` through the in-process adapter contract where supported.
- The public Den MCP `mcp_den_register_worker_run` tool does not yet expose first-class `runtime_registry`, `runtime_id`, or `registry_fingerprint` parameters, so the bridge must not send unknown fields to the live MCP tool.

Decision:

- Do not overload `profile`, `provider`, `model`, `state_file_ref`, or `prompt_packet_message_id` with registry audit metadata.
- Keep the bridge sanitized and ready, but require a Den Core/MCP schema extension before those fields become authoritative in live Den worker records.
- Target Den fields should be explicit and queryable, for example:

```json
{
  "runtime_registry": {
    "registry_id": "den-hermes-runner-defaults",
    "registry_fingerprint": "sha256:...",
    "runtime_id": "coder-primary",
    "role": "coder",
    "resolved_at": "2026-05-13T00:00:00Z",
    "override": null
  }
}
```

Security rule:

- Never store API keys, `.env` contents, `auth.json`, credential pool details, bearer tokens, or raw environment dumps in registry audit metadata.

## #1393 reviewer test evidence decision

Reviewer completion artifacts may include `tests_run` when the reviewer executed deterministic checks as part of review. The bridge now forwards that evidence into `post_worker_completion_packet` for `review_findings_packet` completions when present:

```json
[
  {"command": "python -m pytest tests/ -q", "result": "54 passed"}
]
```

This mirrors the coder packet behavior and lets future Den `latest_completion` / worker status displays show the reviewer's own verification evidence separately from coder implementation tests.

## Validation

Targeted regression tests cover:

- The default registry path resolves to `/home/agents/runtime/spawned-hermes-runtimes.yaml`.
- Reviewer completion packets include `tests_run` when supplied in the reviewer artifact.
- Existing runtime resolver/operator/spawned-worker tests continue to pass.

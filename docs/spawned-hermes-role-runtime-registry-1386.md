# Spawned-Hermes role runtime registry design

Task: `den-hermes-bridge` #1386  
Parent: #1385  
Status: design contract for #1387-#1390

## 1. Problem

The tracked `spawned_hermes` path now has Den worker-run registration, process launch, local artifact verification, completion reconciliation, lifecycle cleanup/rerun support, and a successful live profile smoke. The next problem is operator ergonomics: coder/reviewer/drift/validator runtime choices change often as providers, models, pricing, rate limits, and quality move around.

Patch explicitly does **not** want hidden per-project provider/model overrides as the normal mechanism. A project-local stale override can leave one project using a goofy old model and is easy to forget. The default should be one central place where the current role picks are changed, inspected, preflighted, and audited.

## 2. Decision summary

Use a **central spawned-Hermes role-runtime registry** resolved by `den-hermes-bridge` before every tracked worker launch.

Recommended active registry path on this fleet:

```text
/home/agents/runtime/spawned-hermes-runtimes.yaml
```

Recommended repo-managed reference/sample path:

```text
config/spawned-hermes-runtimes.sample.yaml
```

Recommended environment/path override:

```text
DEN_HERMES_RUNTIME_REGISTRY=/absolute/path/to/spawned-hermes-runtimes.yaml
```

The active registry is central to the bridge profile/fleet, not per Den project. Den task/project metadata records resolved runtime values for audit only; it does not silently change the selected coder/reviewer/drift models.

A Den document should mirror the current registry contract and optionally the sanitized active matrix for operator visibility, but the local YAML file is the resolver's authoritative runtime input. This keeps worker launch deterministic and available to local processes without requiring the resolver to fetch/parse Den docs during the launch hot path.

## 3. Non-goals

- Do not make Hermes built-in `delegate_task` the durable Den worker substrate.
- Do not add per-project provider/model defaults as the normal path.
- Do not store API keys, `.env`, `auth.json`, provider credential pools, or full profile configs in the registry or Den worker metadata.
- Do not encode task/review/orchestrator policy in the registry. It only chooses runtime launch settings.
- Do not let subprocesses guess their own profile/provider/model. The launcher resolves and records them before launch.

## 4. Registry ownership and lifecycle

### 4.1 Active registry

The active registry is an operator-editable YAML file under the shared agent runtime area:

```text
/home/agents/runtime/spawned-hermes-runtimes.yaml
```

Why this location:

- It is central for this bridge runner profile and not tied to any one Den project.
- It is separated from profile credential/auth state while still living under the shared `/home/agents` operational tree.
- It is easy for operators to edit without changing Den task metadata.
- It can be backed up, diffed, and copied between runner hosts.
- It keeps runtime launch independent of Den doc availability during outage/degraded periods.

### 4.2 Repo sample/default

The repo should carry a sample/default file at:

```text
config/spawned-hermes-runtimes.sample.yaml
```

The sample exists for review, tests, bootstrap, and documentation. It is not automatically authoritative in production unless explicitly selected by `DEN_HERMES_RUNTIME_REGISTRY` or a CLI flag.

### 4.3 Den doc mirror

Store a Den document such as:

```text
den-hermes-bridge/spawned-hermes-role-runtime-registry-1386
```

Use it to document the schema, expected operator workflow, and sanitized active matrix. Treat it as visibility and design documentation, not hidden runtime state. If a future Den config-record API exists, it can replace the local YAML only if it gives the bridge a deterministic local cache and clear version/ETag semantics.

## 5. Precedence rules

Precedence should be boring and fail-closed:

1. **Explicit emergency per-run override** only if the caller supplies all of:
   - `allow_runtime_override=true`;
   - `override_reason` non-empty;
   - `requested_by` recorded;
   - every overridden field recorded in Den launch metadata.
2. **Central role entry** from the active registry, e.g. `roles.coder`.
3. **Central defaults** from the active registry, e.g. `defaults.toolsets`, `defaults.timeout_seconds`.
4. **Hard failure** if required fields remain unresolved.

Not in precedence:

- Den project metadata.
- Den task tags.
- Parent umbrella/task descriptions.
- Ambient Hermes default profile/provider/model.
- Shell environment values for provider/model. Environment may point to the registry file, but should not silently supply runtime picks.

Per-run overrides are for debugging/outage escape hatches, not the normal operator path. They must be loud in logs and Den metadata.

## 6. Required role entries

The registry must support these tracked worker roles:

- `coder`
- `reviewer`
- `validator`
- `drift_checker`
- `packet_auditor`

Role aliases may exist for operator ergonomics but must resolve to one of the canonical Den roles before registration.

Suggested aliases:

```yaml
role_aliases:
  drift: drift_checker
  audit: packet_auditor
```

## 7. Schema

### 7.1 Top-level shape

```yaml
schema_version: 1
registry_id: den-hermes-runner-defaults
updated_at: "2026-05-13T00:00:00Z"
updated_by: patch
notes: >-
  Central spawned-Hermes runtime picks for Den orchestrator roles.

defaults:
  substrate: spawned_hermes
  hermes_binary: hermes
  run_root: /tmp/den-hermes
  artifact_filename: completion.json
  log_filename: worker.log
  profile_required: true
  provider_required: true
  model_required: true
  timeout_seconds: 900
  toolsets: [terminal, file]
  workdir: /home/dev/den-hermes
  preflight:
    enabled: true
    prompt: "Reply with exactly: PROFILE_OK"
    expected_substring: PROFILE_OK
    timeout_seconds: 300
  audit:
    record_registry_id: true
    record_registry_version: true
    record_role_runtime_id: true
    record_resolved_at: true
    record_override_reason: true

roles:
  coder:
    runtime_id: coder-primary
    profile: den-hermes-runner
    provider: openai-codex
    model: gpt-5.5
    toolsets: [terminal, file]
    timeout_seconds: 1800
    reasoning_effort: high
    max_retries: 1
    preflight:
      enabled: true
    launch:
      source: den-worker
      extra_args: []

  reviewer:
    runtime_id: reviewer-primary
    profile: den-hermes-runner
    provider: openai-codex
    model: gpt-5.5
    toolsets: [terminal, file]
    timeout_seconds: 1500
    reasoning_effort: high
    max_retries: 1
    preflight:
      enabled: true
    launch:
      source: den-worker
      extra_args: []

  validator:
    runtime_id: validator-primary
    profile: den-hermes-runner
    provider: openai-codex
    model: gpt-5.5
    toolsets: [terminal, file]
    timeout_seconds: 1200
    reasoning_effort: medium
    max_retries: 0
    preflight:
      enabled: true
    launch:
      source: den-worker
      extra_args: []

  drift_checker:
    runtime_id: drift-checker-primary
    profile: den-hermes-runner
    provider: openai-codex
    model: gpt-5.5
    toolsets: [terminal, file]
    timeout_seconds: 900
    reasoning_effort: medium
    max_retries: 0
    preflight:
      enabled: true
    launch:
      source: den-worker
      extra_args: []

  packet_auditor:
    runtime_id: packet-auditor-primary
    profile: den-hermes-runner
    provider: openai-codex
    model: gpt-5.5
    toolsets: [terminal, file]
    timeout_seconds: 900
    reasoning_effort: medium
    max_retries: 0
    preflight:
      enabled: true
    launch:
      source: den-worker
      extra_args: []

role_aliases:
  drift: drift_checker
  audit: packet_auditor
```

### 7.2 Field meanings

| Field | Required | Meaning |
| --- | --- | --- |
| `schema_version` | yes | Registry schema. Start at `1`. Unknown major versions fail closed. |
| `registry_id` | yes | Stable identifier recorded in Den launch metadata. |
| `updated_at` / `updated_by` | recommended | Human/operator trace. Not security authority. |
| `defaults.substrate` | yes | Must be `spawned_hermes` for this registry. |
| `defaults.hermes_binary` | yes | Executable name/path, usually `hermes`. |
| `defaults.run_root` | yes | Root for per-run artifacts/logs. |
| `defaults.profile_required` | yes | Should remain true; spawned workers must use explicit profiles. |
| `defaults.provider_required` / `model_required` | yes | Should remain true unless a role explicitly opts into profile defaults. |
| `roles.<role>.runtime_id` | yes | Stable runtime choice label, e.g. `coder-primary`. |
| `roles.<role>.profile` | yes | Hermes profile passed as `--profile`. |
| `roles.<role>.provider` | yes by default | Provider passed as `--provider`. Name only, no keys. |
| `roles.<role>.model` | yes by default | Model passed as `--model`. |
| `roles.<role>.toolsets` | yes | Hermes toolsets for this role. Prefer narrow role-specific sets. |
| `roles.<role>.timeout_seconds` | yes | Subprocess timeout before bridge failure handling. |
| `roles.<role>.reasoning_effort` | optional | Recorded for future CLI/provider support; ignored if launcher cannot use it. |
| `roles.<role>.max_retries` | optional | Launcher/orchestrator retry budget, not Den retry policy. |
| `roles.<role>.preflight` | optional | Whether operator preflight should test this role. |
| `roles.<role>.launch.source` | optional | Hermes `--source`, default `den-worker`. |
| `roles.<role>.launch.extra_args` | optional | Safe non-secret CLI args, allowlisted by resolver. |

## 8. Resolved runtime object

The resolver should return a typed object that is already safe to hand to launcher/Den adapter:

```json
{
  "schema_version": 1,
  "registry_id": "den-hermes-runner-defaults",
  "registry_path": "/home/agents/runtime/spawned-hermes-runtimes.yaml",
  "registry_fingerprint": "sha256:...",
  "resolved_at": "2026-05-13T00:00:00Z",
  "role": "coder",
  "runtime_id": "coder-primary",
  "substrate": "spawned_hermes",
  "hermes_binary": "hermes",
  "profile": "den-hermes-runner",
  "provider": "openai-codex",
  "model": "gpt-5.5",
  "toolsets": ["terminal", "file"],
  "timeout_seconds": 1800,
  "workdir": "/home/dev/den-hermes",
  "run_root": "/tmp/den-hermes",
  "artifact_filename": "completion.json",
  "log_filename": "worker.log",
  "source": "den-worker",
  "extra_args": [],
  "preflight": {
    "enabled": true,
    "prompt": "Reply with exactly: PROFILE_OK",
    "expected_substring": "PROFILE_OK",
    "timeout_seconds": 300
  },
  "override": null
}
```

Required fail-closed validation:

- `role` resolves to canonical known role.
- `profile` exists and is non-empty.
- `provider` and `model` exist unless the role explicitly opts into profile defaults with a loud flag such as `use_profile_model_defaults: true`.
- `toolsets` is a non-empty list of strings.
- `timeout_seconds` is positive and under a configured max.
- `run_root` and `workdir` are absolute paths.
- `extra_args` contains only allowlisted non-secret flags.
- No field value looks like a credential key/token; fail or redact in diagnostics.

## 9. Den registration/audit metadata

`mcp_den_register_worker_run` already accepts `profile`, `provider`, `model`, `toolsets`, `workdir`, `timeout_seconds`, `artifact_path`, `log_path`, and other launch metadata.

The launcher should record at least:

| Den field / metadata | Source |
| --- | --- |
| `profile` | resolved runtime |
| `provider` | resolved runtime |
| `model` | resolved runtime |
| `toolsets` | resolved runtime, serialized as CSV if required by tool schema |
| `workdir` | resolved runtime or launch context |
| `timeout_seconds` | resolved runtime |
| `artifact_path` | `run_root/run_id/artifact_filename` |
| `log_path` | `run_root/run_id/log_filename` |
| `host` | launcher host |
| `dedupe_key` | task/role/run id |
| `launch_profile_json` or equivalent | sanitized resolved runtime subset |

Sanitized runtime audit payload should include:

```json
{
  "runtime_registry": {
    "registry_id": "den-hermes-runner-defaults",
    "registry_fingerprint": "sha256:...",
    "runtime_id": "coder-primary",
    "role": "coder",
    "resolved_at": "2026-05-13T00:00:00Z",
    "override": null
  },
  "runtime": {
    "profile": "den-hermes-runner",
    "provider": "openai-codex",
    "model": "gpt-5.5",
    "toolsets": ["terminal", "file"],
    "timeout_seconds": 1800,
    "source": "den-worker"
  }
}
```

Do not record API keys, env, `auth.json`, `.env`, or full profile config.

## 10. Operator workflow

### 10.1 Inspect active matrix

Future CLI (#1389) should support:

```bash
den-hermes runtime matrix
```

Example output:

```text
Registry: den-hermes-runner-defaults (/home/agents/runtime/spawned-hermes-runtimes.yaml)
Fingerprint: sha256:...

ROLE            PROFILE             PROVIDER       MODEL     TOOLSETS        TIMEOUT  RUNTIME
coder           den-hermes-runner    openai-codex   gpt-5.5   terminal,file   1800s    coder-primary
reviewer        den-hermes-runner    openai-codex   gpt-5.5   terminal,file   1500s    reviewer-primary
validator       den-hermes-runner    openai-codex   gpt-5.5   terminal,file   1200s    validator-primary
drift_checker   den-hermes-runner    openai-codex   gpt-5.5   terminal,file   900s     drift-checker-primary
packet_auditor  den-hermes-runner    openai-codex   gpt-5.5   terminal,file   900s     packet-auditor-primary
```

### 10.2 Edit central picks

Future CLI should print the path or open the configured file:

```bash
den-hermes runtime path
den-hermes runtime edit
```

Manual edit is acceptable for v1. The validation/preflight command is the guardrail.

### 10.3 Validate schema

```bash
den-hermes runtime validate
```

Checks:

- YAML parses.
- Required roles exist.
- Required fields resolve.
- Unknown role names and unknown top-level keys are warnings or errors according to schema mode.
- Paths are absolute where required.
- Toolsets are list/CSV-safe.
- `profile`, `provider`, `model` are non-empty.
- No secret-looking values are present.

### 10.4 Preflight profiles/models

```bash
den-hermes runtime preflight --roles coder,reviewer
```

For each selected role, run the exact resolved profile/provider/model/toolset shape with a harmless prompt:

```bash
hermes --profile den-hermes-runner chat \
  --provider openai-codex \
  --model gpt-5.5 \
  --toolsets '' \
  --source den-runtime-preflight \
  -q 'Reply with exactly: PROFILE_OK'
```

Preflight success requires exit code 0 and expected substring. Preflight should not write Den worker completion packets because it is not a Den worker run.

## 11. Launcher integration contract for #1388

Before registering a worker:

1. Resolve role runtime from registry.
2. Compute artifact/log paths using run id.
3. Register Den worker run with resolved profile/provider/model/toolsets/timeout and sanitized runtime audit metadata.
4. Fail closed if Den registration rejects/mismatches.
5. Launch `hermes` with the exact resolved runtime:

```bash
hermes --profile <profile> chat \
  --provider <provider> \
  --model <model> \
  --toolsets <comma-separated-toolsets> \
  --source den-worker \
  -q <bounded prompt with artifact contract>
```

If `use_profile_model_defaults: true` is ever allowed for a role, the launcher still records that fact and must preflight the profile. It should be rare because it hides the model pick in Hermes profile config, which weakens the central registry goal.

## 12. Acceptance/test plan for #1387 resolver

Use strict TDD. Suggested tests:

1. `test_resolver_loads_required_roles_from_registry`
   - Given sample YAML with all roles.
   - Resolving `coder` returns profile/provider/model/toolsets/timeout and registry metadata.

2. `test_resolver_applies_central_defaults`
   - Role omits `run_root` and `artifact_filename`.
   - Resolved runtime inherits defaults.

3. `test_resolver_rejects_missing_required_role`
   - Registry lacks `reviewer`.
   - Loading or validation fails with a message naming `reviewer`.

4. `test_resolver_rejects_missing_explicit_profile`
   - Role lacks `profile` while `profile_required: true`.
   - Resolution fails before launcher can spawn.

5. `test_resolver_rejects_missing_provider_or_model_by_default`
   - Role lacks provider/model.
   - Resolution fails unless `use_profile_model_defaults: true` is explicitly set.

6. `test_resolver_rejects_project_local_override_without_escape_hatch`
   - Caller supplies project/task metadata override.
   - Resolver ignores/rejects it unless explicit emergency override fields are present.

7. `test_resolver_allows_audited_emergency_override`
   - Caller supplies `allow_runtime_override`, `override_reason`, and new provider/model.
   - Resolved runtime includes override block and audit metadata.

8. `test_resolver_normalizes_aliases_to_canonical_roles`
   - Resolving `drift` returns canonical `drift_checker`.

9. `test_resolver_generates_artifact_and_log_paths_from_run_id`
   - Given run id and run root.
   - Paths are `/tmp/den-hermes/<run_id>/completion.json` and `/tmp/den-hermes/<run_id>/worker.log`.

10. `test_resolver_redacts_or_rejects_secret_like_values`
    - Registry includes key-looking field/value.
    - Validation fails without echoing the full secret.

11. `test_resolved_runtime_serializes_to_den_registration_args`
    - Resolved runtime becomes `profile`, `provider`, `model`, CSV `toolsets`, `timeout_seconds`, `artifact_path`, and `log_path` args.

12. `test_preflight_command_uses_exact_resolved_profile_provider_model`
    - Preflight command includes explicit `--profile`, `--provider`, `--model`, `--toolsets`, and harmless prompt.

## 13. Acceptance/test plan for #1388 launcher wiring

1. Existing launcher tests keep passing.
2. A new fake registry test proves `run_den_coder_reviewer_workflow` can accept role names instead of inline coder/reviewer dicts, or a wrapper resolves before calling current functions.
3. Den registration happens with resolved runtime values.
4. Subprocess command uses resolved runtime values.
5. Den registration failure still prevents launch.
6. Explicit per-run override without reason fails before launch.
7. Explicit per-run override with reason records override metadata in Den registration.

## 14. Operational tradeoffs

### Local YAML as active registry

Pros:

- Fast and deterministic.
- Easy to edit/preflight.
- Works even when Den docs are unavailable.
- Keeps model picks out of project/task metadata.

Cons:

- Needs backup/deployment discipline across runner hosts.
- Operators must remember to mirror important changes to Den doc if human visibility matters.

Mitigation: CLI can show fingerprint and optionally write a sanitized Den note after successful validation/preflight.

### Den document/config as active registry

Pros:

- Visible in Den.
- Central across machines.
- Auditable with Den docs.

Cons:

- Launch hot path now depends on Den doc availability/parsing.
- Harder to safely edit without accidentally creating bad runtime config.
- Existing Den document storage is not a typed config API.

Recommendation: use Den docs as mirror/design until a typed Den config-record API exists.

### Hermes profile config as active registry

Pros:

- Hermes already knows profiles/provider/model.

Cons:

- Role picks are spread across profiles and hidden from Den orchestration.
- Hard to show a single role matrix.
- Encourages profile-default model inheritance, which caused the earlier DeepSeek missing-key failure.

Recommendation: use profiles for credentials/personality/tool availability, not as the primary role-pick registry.

## 15. Final recommendation

Implement #1387 as a small Python resolver over one central YAML registry. Keep the active registry in the shared agent runtime area, ship a repo sample, mirror the design/sanitized matrix in Den docs, and make all launcher paths resolve role runtimes before Den registration.

The normal operator move should be:

```bash
den-hermes runtime edit
den-hermes runtime validate
den-hermes runtime preflight --all
den-hermes runtime matrix
```

Then all subsequent coder/reviewer/validator/drift/packet-auditor launches use the new central picks and record the resolved profile/provider/model/toolsets in Den worker-run metadata for audit.

## 16. Launcher integration example (#1388)

The first launcher integration resolves coder/reviewer runtimes when `run_den_coder_reviewer_workflow(...)` receives a registry path:

```python
result = run_den_coder_reviewer_workflow(
    den_client=den,
    task_id=1388,
    prompt="Use the bounded Den packet context.",
    run_root="/tmp/den-hermes",
    cwd="/home/dev/den-hermes",
    coder={"run_id": "coder-run"},
    reviewer={"run_id": "reviewer-run"},
    runtime_registry_path="/home/agents/runtime/spawned-hermes-runtimes.yaml",
)
```

With the registry enabled:

- `coder` and `reviewer` only need run identity at the call site; profile/provider/model/toolsets/timeouts come from the central registry.
- The Hermes subprocess command is explicit: `hermes --profile <resolved-profile> chat --provider <resolved-provider> --model <resolved-model> --toolsets <resolved-toolsets> ...`.
- Den registration receives the resolved profile/provider/model/toolsets/timeout and the local bridge records the resolved `runtime_id` when the injected Den client supports it.
- Hidden per-call runtime fields such as `coder={"provider": "..."}` are rejected unless they are declared as audited emergency overrides with `allow_runtime_override`, `override_reason`, and `requested_by`.
- Resolver failures happen before Den registration and before any subprocess launch.

Existing low-level tests still support explicit inline profile/provider/model values for fake/manual flows without a registry path. Durable Den orchestrator paths should pass the registry path (or use `DEN_HERMES_RUNTIME_REGISTRY` once launcher wiring is promoted to always-on central resolution).

## 17. Operator command surface (#1389)

The initial operator surface is available as a Python module:

```bash
python -m den_hermes.runtime_ops --registry config/spawned-hermes-runtimes.sample.yaml matrix
python -m den_hermes.runtime_ops --registry config/spawned-hermes-runtimes.sample.yaml validate
python -m den_hermes.runtime_ops --registry config/spawned-hermes-runtimes.sample.yaml preflight --roles coder,reviewer
```

`matrix` prints the active profile/provider/model/toolset/timeout/runtime-id table. `validate` resolves all required roles and fails closed on malformed config. `preflight` runs the exact resolved Hermes profile/provider/model shape with a harmless `PROFILE_OK` prompt and redacts secret-looking stderr/stdout values before printing diagnostics.

Current-host preflight evidence for the sample central picks:

```text
python -m den_hermes.runtime_ops --registry config/spawned-hermes-runtimes.sample.yaml preflight --roles coder,reviewer
OK coder profile=den-hermes-runner provider=openai-codex model=gpt-5.5 exit=0
OK reviewer profile=den-hermes-runner provider=openai-codex model=gpt-5.5 exit=0
```

To change the active reviewer pick, edit the active registry file (normally `/home/agents/runtime/spawned-hermes-runtimes.yaml`), run `validate`, then run `preflight --roles reviewer`. Do not edit Den project/task metadata to change runtime picks.

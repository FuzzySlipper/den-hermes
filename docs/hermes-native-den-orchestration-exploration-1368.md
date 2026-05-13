# Hermes-native Den orchestration via subagents — exploration for task #1368

## Scope and framing

This note explores how `den-hermes-bridge` could run Den coder/reviewer/validator-style workflows using Hermes-native subagents or spawned Hermes processes instead of the current Den Pi/tmux/docker worker substrate.

I did **not** run the full orchestrator workflow or launch real Den coder/reviewer workers for this exploration. The work here is source/document inspection plus an architecture comparison, per the user request to research the shape before trying the workflow.

References inspected:

- Hermes source: `/home/agents/hermes-agent/tools/delegate_tool.py`
- Hermes tests: `/home/agents/hermes-agent/tests/tools/test_delegate.py`
- Den global docs: `_global/pi-orchestrator-guidance-default`, `_global/pi-coder-subagent-prompt-default`, `_global/pi-reviewer-subagent-prompt-default`, `_global/agent-review-loop-policy`
- Pi examples: `/home/dev/den-pi/skills/den-orchestrator/SKILL.md`, `/home/dev/den-pi/docs/worker-runtime-artifact-contract.md`
- External reference: `witt3rd/oh-my-hermes`, especially `docs/omh-delegate.md`, `docs/hermes-constraints.md`, and `plugins/omh/omh_delegate.py`

## Current Hermes delegation behavior

### Where child agents are constructed

`delegate_task` is implemented in `tools/delegate_tool.py`. The key path is:

1. `delegate_task(...)` validates single-task or batch mode.
2. It calls `_load_config()` to read `delegation.*` settings.
3. It calls `_resolve_delegation_credentials(cfg, parent_agent)` once per delegate call.
4. It builds each child with `_build_child_agent(...)`.
5. Single task runs synchronously via `_run_single_child(...)`; batch uses a `ThreadPoolExecutor` and still blocks the parent until all children finish or the parent is interrupted.
6. The parent receives only a summarized JSON result: `status`, `summary`, `api_calls`, `duration_seconds`, `model`, token counts, a compact `tool_trace`, and a few observability fields.

Important source anchors:

- `delegate_task` signature and flow: `tools/delegate_tool.py:1898-2289`
- child construction: `tools/delegate_tool.py:865-1158`
- synchronous child execution and timeout handling: `tools/delegate_tool.py:1305-1873`
- credential resolution: `tools/delegate_tool.py:2325-2422`
- dynamic schema: `tools/delegate_tool.py:2455-2767`

### Model/provider resolution today

Hermes already has config-scoped delegation routing:

- `delegation.model`
- `delegation.provider`
- `delegation.base_url`
- `delegation.api_key`
- `delegation.reasoning_effort`
- `delegation.max_iterations`
- `delegation.max_concurrent_children`
- `delegation.child_timeout_seconds`
- `delegation.max_spawn_depth`
- `delegation.orchestrator_enabled`

`_resolve_delegation_credentials()` reads `delegation.model/provider/base_url/api_key`. If `delegation.provider` is set, it calls `hermes_cli.runtime_provider.resolve_runtime_provider(requested=configured_provider, target_model=configured_model)`, which is the same family of provider resolution used by CLI/gateway startup. If no delegation provider/base URL is configured, children inherit provider, base URL, API key, API mode, and model from the parent, except `delegation.model` can override model while inheriting provider credentials.

`_build_child_agent()` then constructs a fresh `AIAgent` with the effective model/provider/base URL/API key/API mode and its own toolset, prompt, terminal session, token/cost counters, and child progress callback.

### Per-call model/provider override is not in the normal tool schema

The current model-callable `delegate_task` schema exposes:

- top-level: `goal`, `context`, `toolsets`, `tasks`, `role`, `acp_command`, `acp_args`
- per-task batch entries: `goal`, `context`, `toolsets`, `acp_command`, `acp_args`, `role`

It does **not** expose top-level or per-task `provider`, `model`, `base_url`, or `api_key` fields for normal LLM providers. ACP command overrides are distinct and should not be confused with normal provider/model routing.

That means the initial Den requirement — run coder with model/provider A, then reviewer with model/provider B — is not directly expressible as two `delegate_task(...)` calls in the same parent session unless the runner mutates config or uses a lower-level/internal wrapper instead of the public tool schema.

### Can delegation settings change safely between sequential delegate calls?

Source says `_load_config()` is called on each `delegate_task` invocation, not only at process startup. It first checks `cli.CLI_CONFIG["delegation"]`, then falls back to persistent `hermes_cli.config.load_config()`.

That technically means config-scoped changes could be observed between two sequential calls. However, I would not design Den orchestration around mutating `config.yaml` or `CLI_CONFIG` between coder and reviewer roles:

- It is session-global, not role/run-scoped.
- It is racy if multiple delegations or parent turns overlap.
- It is hard to audit in Den as a per-worker launch attribute.
- It is surprising to the parent LLM because the tool schema does not show the changed provider/model at the call site.
- It risks prompt/cache/tool stability assumptions around current-session config.

Use config-scoped delegation for a profile default, not for per-role Den runtime selection.

### What delegate_task covers well

Hermes `delegate_task` gives us several useful properties out of the box:

- fresh child conversation with no parent history;
- restricted toolsets inherited/intersected from the parent;
- blocked tools for leaves (`delegate_task`, `clarify`, `memory`, `send_message`, `execute_code`);
- optional nested delegation through `role="orchestrator"` when `delegation.max_spawn_depth >= 2`;
- separate terminal/file state per child;
- parent-context protection: only final summaries/tool trace are returned;
- child timeout, interrupt propagation, and basic TUI/gateway progress events;
- token/cost rollup from children into the parent;
- dynamic schema descriptions for configured concurrency/depth limits;
- plugin hook `subagent_stop` after each child.

### Important delegate_task limitations for Den workers

For Den worker semantics, the key limitation is not intelligence; it is lifecycle/durability.

`delegate_task` is synchronous inside the parent run. Its own model-facing description states the durable-work warning explicitly: if the parent is interrupted, stopped, or reset, children are cancelled/interrupted and cannot continue in the background. Intermediate child tool results do not enter parent context; only the final summary arrives.

Those properties are good for local reasoning fan-out, but they do not match the current Den Pi worker contract where worker runs are durable Den records with status, packet refs, cleanup, rerun/abort, and operator-observable lifecycle independent of the orchestrator turn.

## Lessons from Den Pi and oh-my-hermes

### Den Pi contract to preserve

The Pi docs are useful as a contract description even if we stop using Pi as the substrate:

- orchestrator prepares bounded context packets and stores them in Den;
- worker launch passes small references and env/state handles, not large prompt bodies/secrets;
- worker identifies project/task/run/session/role;
- worker posts structured completion packets (`implementation_packet`, `review_findings_packet`, `validation_packet`, etc.);
- orchestrator reconciles packet claims against Den state, branch/head, tests, review rounds, and findings;
- process exit alone is not success;
- cleanup/rerun/abort/status are first-class lifecycle operations.

Pi is mostly an execution/runtime boundary. The Den packet/review-state contract should survive a Hermes-native implementation.

### oh-my-hermes patterns worth borrowing, not porting wholesale

`oh-my-hermes` has a hardened wrapper around `delegate_task` (`omh_delegate`) that exists because Hermes subagents normally return only final summaries. It uses a prepare/finalize split:

1. prepare an expected output path and breadcrumb;
2. append a strict “write your artifact here” contract to the child goal;
3. dispatch via the normal Hermes `delegate_task` tool;
4. finalize by checking the artifact file exists and recording completion breadcrumbs.

This is directly relevant to Den because Den needs reliable receipts, not just prose summaries. The pattern to borrow is **subagent-persisted receipts plus parent verification**. The path/storage should be Den-native (completion packet, worker run/event, state ref), not `.omh` project state.

## Candidate approaches

### Option A — config-scoped sequential delegate_task calls

**Shape:** Configure `delegation.provider/model` for coder, call `delegate_task`, mutate config for reviewer, call `delegate_task` again.

**Pros:**

- no Hermes code changes;
- uses the existing child `AIAgent` path and credential resolution;
- viable for a personal/manual smoke test.

**Cons:**

- role-specific provider/model is hidden global mutable state;
- not safe for concurrent orchestrator sessions;
- difficult to audit/replay as Den worker launch metadata;
- likely brittle in gateway/CLI sessions where config may be snapshotted or cached in `CLI_CONFIG`;
- does not solve durability/background/status/cleanup.

**Recommendation:** Do not use as a production Den bridge. It is acceptable only as a quick manual experiment.

### Option B — extend delegate_task with per-call provider/model overrides

**Shape:** Add optional provider/model fields to `delegate_task`, initially single-task only or both top-level and per-task:

```json
{
  "goal": "Run coder role for Den task #1368",
  "context": "...bounded Den packet refs...",
  "toolsets": ["terminal", "file", "mcp-den"],
  "provider": "openrouter",
  "model": "anthropic/claude-sonnet-4",
  "role": "leaf"
}
```

For batch mode, allow per-task overrides later if needed. Den coder/reviewer execution is sequential, so top-level single-task support is enough for the first increment.

Implementation sketch in Hermes:

1. Extend `delegate_task(...)` signature and schema with optional `provider`, `model`, maybe `base_url` but probably **not** `api_key` in model-callable schema.
2. Add an override resolver similar to `_resolve_delegation_credentials(cfg, parent_agent)`, but fed by explicit call args with fallback to config.
3. Preserve existing behavior when overrides are absent.
4. Keep ACP override precedence explicit: if `acp_command` is set, ACP wins and provider becomes `copilot-acp`; normal provider/model fields should not silently mix with ACP transport.
5. Clear inherited OpenRouter provider filters when an explicit provider override is supplied, matching the current config override behavior.
6. Add tests for inheritance, model-only override, provider+model override, provider failure, ACP precedence, and batch behavior.
7. Ensure dynamic tool description mentions the feature and its safety constraints.

**Pros:**

- natural use of Hermes' built-in subagent isolation;
- exact per-role provider/model at call site;
- minimal process overhead;
- fits quick local, synchronous coder/reviewer experimentation;
- improves upstream Hermes for other multi-agent use cases.

**Cons:**

- still synchronous and parent-turn-bound;
- child cannot post Den messages if `send_message` remains blocked, though Den MCP tools may be inherited depending on toolset config;
- child output still needs explicit receipt handling because summary-only is intentional;
- exposing arbitrary `base_url`/`api_key` in model-callable schema would be risky; better to use configured provider names/profiles, not raw secrets.

**Recommendation:** Good upstream/Hermes improvement, but insufficient by itself as the entire Den worker substrate unless Den accepts synchronous, non-durable worker semantics for some classes of work.

### Option C — spawned Hermes local workers

**Shape:** `den-hermes-bridge` launches a separate Hermes process per role/run, for example:

```bash
hermes chat --provider <provider> --model <model> \
  --toolsets terminal,file,mcp-den \
  -q "$DEN_WORKER_STARTUP_PROMPT"
```

or a more structured local runner invokes Hermes with a state-file or prompt-packet reference and env vars analogous to Den Pi:

- `DEN_WORKER_PROJECT_ID`
- `DEN_WORKER_TASK_ID`
- `DEN_WORKER_RUN_ID`
- `DEN_WORKER_ROLE`
- `DEN_WORKER_PROMPT_PACKET_MESSAGE_ID`
- `DEN_WORKER_STATE_FILE_REF`
- `DEN_WORKER_EXPECTED_COMPLETION_PATH`

**Pros:**

- per-role `--provider` / `--model` works today at process boundary;
- can run in background with OS process lifecycle;
- durable Den worker run records can map to process/session IDs;
- easier to abort/rerun/cleanup than in-process `delegate_task`;
- closer to current Pi worker semantics without Docker/tmux/Pi;
- child has a full Hermes profile/process, so tool permissions can be role/profile-specific.

**Cons:**

- more launch overhead than in-process subagents;
- process supervision and log capture must be implemented in `den-hermes-bridge`;
- still needs credential/profile isolation design;
- a plain one-shot `hermes chat -q` is not enough by itself: the bridge must capture stdout/stderr/session ID and reconcile completion packets;
- if run under the same profile, workers may share broad credentials/tools unless role-specific profiles or scoped toolsets are enforced.

**Recommendation:** Best first bridge if the goal is to replace Den Pi/tmux/docker while keeping Den worker lifecycle semantics.

### Option D — hybrid staged path

**Shape:** Build a Den-local Hermes worker runner using spawned Hermes processes first. In parallel or later, upstream per-call provider/model overrides to `delegate_task` and use direct subagents for short-lived non-durable checks.

**Pros:**

- preserves Den safety/lifecycle contracts now;
- does not block on Hermes upstream changes;
- still lets `den-hermes-bridge` dogfood and influence native delegation improvements;
- supports both durable worker runs and cheap synchronous helper subtasks.

**Cons:**

- two substrates to explain;
- must be clear when Den chooses durable process vs synchronous subagent;
- some duplicate prompt/packet shaping unless factored carefully.

**Recommendation:** This is the strongest direction.

## Proposed Den-facing contract for Hermes-native workers

Whether implemented as direct `delegate_task` or spawned Hermes, `den-hermes-bridge` should expose the same conceptual contract:

```yaml
project_id: den-hermes-bridge
role: coder | reviewer | validator | drift_checker | packet_auditor
task_id: 1368
run_id: <den-worker-run-id>
session_id: <hermes-session-or-process-id>
provider: <configured provider name>
model: <configured model name>
toolsets: [terminal, file, mcp-den]
workdir: /home/dev/den-hermes
branch: task/1368-hermes-native-delegation-exploration
prompt_packet_message_id: <Den message id>
state_file_ref: <optional local state file>
expected_completion_packet: implementation_packet | review_findings_packet | ...
timeout_seconds: <role default>
```

Completion should always be reconciled through Den:

- worker final prose is advisory;
- completion packet or structured artifact is authoritative;
- orchestrator verifies branch/head/tests/review state before advancing;
- missing/malformed completion is infrastructure/workflow failure, not success.

For direct `delegate_task`, use the oh-my-hermes-style receipt pattern adapted to Den:

- parent prepares a Den worker run row or pseudo-run row;
- parent injects explicit instructions to post or write a completion packet receipt;
- parent finalizes by reading the Den packet/artifact, not trusting the child summary;
- parent records failure if the child returns but the receipt is missing.

## Recommended staged implementation plan

### Stage 1 — no Hermes upstream changes: local spawned-Hermes worker runner

Implement in `den-hermes-bridge`:

1. A small CLI/module, e.g. `den_hermes_worker_runner`, that accepts project/task/role/model/provider/workdir/packet-ref.
2. A launch record in Den or a local bridge record mapped to Den worker run IDs.
3. A bounded startup prompt that points at Den packet refs, not raw giant context.
4. Role profiles/config presets for coder/reviewer/validator.
5. Process supervision using Hermes process boundaries; do not depend on tmux as the API.
6. Completion packet verification using Den tools/API.
7. Tests with a fake `hermes` executable that writes expected completion artifacts, so lifecycle logic is tested without real model calls.

This gives per-role provider/model today and preserves durable lifecycle semantics.

### Stage 2 — upstream/Hermes improvement: provider/model override for delegate_task

Add per-call `provider`/`model` support to `delegate_task`, with tests, but initially use it for short-lived synchronous helper work and optional smoke experiments.

Avoid exposing raw `api_key` to the model-callable schema. If custom base URL is needed, prefer configured named providers or a non-model-callable bridge API that resolves safe provider profiles.

### Stage 3 — Den-native subagent receipt wrapper

Create a bridge-level wrapper pattern equivalent to `omh_delegate_prepare/finalize`, but Den-native:

- `prepare_den_subagent_run(role, task_id, packet_ref, expected_packet_type, provider, model)`
- delegate/spawn
- `finalize_den_subagent_run(run_id)` verifies Den completion packet, branch/head, status, and packet type.

This should work for both substrates.

## Recommendation

Use a **staged hybrid**:

1. For real Den coder/reviewer workflow replacement, prefer **spawned Hermes local workers** first. This matches Den's durable worker lifecycle much better than in-process `delegate_task`, while eliminating Pi/tmux/docker as the worker implementation detail.
2. Add or upstream **per-call provider/model overrides to `delegate_task`** as a useful Hermes-native enhancement, but treat it as the synchronous subagent substrate, not the durable worker substrate.
3. Unify both behind a Den-facing role/run contract and receipt/finalize layer, so the orchestrator state machine does not care whether the worker was Pi, spawned Hermes, or direct `delegate_task`.

The central design rule: keep Den as the source of truth and treat any child/subagent final summary as an untrusted hint until a Den-compatible receipt/completion packet has been verified.

## Open questions to discuss

1. Should `den-hermes-bridge` own a local worker-run table/state file, or should all local spawned-Hermes lifecycle state be recorded directly in Den worker-run APIs?
2. Should role-specific provider/model choices be Den task/project config, Hermes profile config, or explicit launch args?
3. How much Den MCP access should a child Hermes worker receive? Coder/reviewer prompts currently expect Den access, but least-privilege may require scoped Den tools or capability tokens.
4. Do we want upstream `delegate_task(provider/model)` to expose only provider/model, or also safe named `profile`/`runtime` presets?
5. Should direct `delegate_task` ever be allowed for substantial code edits, or only for research/review/analysis where loss of child process durability is acceptable?

## Test plan for next spike

No live workflow was run in this exploration. A safe next spike would be one of:

### Spawned-Hermes bridge spike

- Use a fake `hermes` executable in tests to simulate coder/reviewer outputs.
- Verify the bridge launches coder then reviewer with different provider/model args.
- Verify stdout/stderr/session handles are captured.
- Verify missing completion artifact causes failed/incomplete worker status.
- Verify a good completion packet advances to review request creation only after branch/head/test fields are present.

### Spike A result — 2026-05-12

Implemented the first thin spawned-Hermes launcher spike in commit `130167ab64c7a7ec90405d2e50a5513934147e6f`:

- `den_hermes/worker_launcher.py` adds `run_hermes_worker(...)`, a minimal subprocess runner that invokes `hermes chat`, passes Den worker identity through environment variables, injects an expected-artifact contract into the prompt, captures stdout/stderr/exit code, and validates the returned completion artifact.
- `tests/test_spawned_hermes_worker.py` installs a fake `hermes` executable in a temporary `PATH` and verifies command construction, provider/model/profile/toolset arguments, Den identity environment, stdout capture, missing artifact fail-closed behavior, nonzero exit handling, malformed JSON handling, and mismatched task/run/role identity rejection.
- Validation command: `python -m pytest -q` → `5 passed`.

This confirms the spawned-Hermes bridge surface can be quite small if it is scoped to subprocess launch plus artifact reconciliation. It does not yet implement Den MCP worker-run persistence, review request creation, branch/head/test schema enforcement beyond identity, or real Hermes execution.

### Spike A.2 result — fake coder → reviewer sequence

Implemented the next spawned-Hermes sequence spike in commit `fae403839fd4ab5bd5e92e247cfbbac76f7bf233`:

- `run_hermes_worker(...)` now validates role-specific artifact shape:
  - coder artifacts require `status`, `branch`, full 40-character `head_commit`, `tests_run`, and `summary`;
  - reviewer artifacts require `status`, `verdict`, `findings`, and `summary`.
- `run_coder_reviewer_sequence(...)` runs a coder worker, fails closed if coder completion is invalid, then passes verified coder branch/head/tests into the reviewer prompt.
- The fake `hermes` test harness now appends JSONL call records so tests can assert sequential coder then reviewer launches.
- Tests verify distinct coder/reviewer runtime args: profile, provider, model, and toolsets.
- Validation command: `python -m pytest -q` → `7 passed`.

This still avoids real Hermes/LLM execution and Den MCP side effects, but it proves the local bridge can model the critical sequence boundary: verified coder artifact first, reviewer receives only verified branch/head/test evidence, and runtime selection is explicit per role.

Remaining unimplemented pieces include durable Den worker-run rows/status APIs, Den review-round creation, branch existence checks against git, richer finding schemas, abort/rerun/cleanup controls, and a real Hermes smoke test.

### Spike A.3 result — git verification before reviewer launch

Implemented the next real-testing preparation step in commit `10deca1d072b70c6e89a42b7d7bdc7aa7e5d07d7`:

- `run_coder_reviewer_sequence(..., verify_git=True)` now verifies the coder artifact's `branch` exists as a local git branch before reviewer launch.
- It verifies the reported `head_commit` resolves to a commit and that the branch tip equals that reported head.
- If git verification fails, the sequence fails closed after the coder artifact and does not launch the reviewer.
- Tests create temporary real git repositories to cover both missing-branch blocking and successful branch/head resolution.
- Validation command: `python -m pytest -q` → `9 passed`.

This moves the bridge closer to a real local Hermes smoke test: the reviewer will no longer inspect a claimed branch/head unless the parent can independently resolve that evidence in git.

Remaining unimplemented pieces now include a real Den MCP client adapter, live Den review-round creation, richer reviewer finding schemas, retry loops, abort/rerun/cleanup controls, and a real Hermes/LLM smoke test.

### Spike A.4 result — fake Den lifecycle/review adapter

Implemented the first Den-facing orchestration wrapper around the spawned-Hermes launcher:

- Added `run_den_coder_reviewer_workflow(...)`, which takes a fakeable `den_client` adapter.
- The wrapper records worker lifecycle transitions through the adapter: coder started, coder completed/failed, reviewer started, reviewer completed/failed.
- It requests review only after the coder artifact has passed normal artifact validation plus optional local git verification.
- If coder git verification fails, it records a coder failure, does not request review, and does not launch reviewer.
- Tests use `RecordingDenClient` to assert transition order and prove review request creation is gated by verified coder branch/head/tests evidence.
- Validation command: `python -m pytest -q` → `11 passed`.

This is still intentionally fake-Den: no live Den MCP worker-run rows or review rounds are created. The value is the transition contract. The next step can replace `RecordingDenClient` with a real Den MCP adapter once the lifecycle shape is stable.

Remaining unimplemented pieces now include a real Den MCP client adapter, live Den review-round creation, retry loops, abort/rerun/cleanup controls, and a real Hermes/LLM smoke test.

### Spike A.5 result — reviewer findings adapter transition

Implemented the first reviewer-findings handoff through the fakeable Den adapter:

- The fake reviewer can now emit non-empty `findings` and a configurable `verdict`.
- `run_den_coder_reviewer_workflow(...)` calls `den_client.post_review_findings(...)` after reviewer artifact validation and lifecycle completion recording.
- The adapter call includes task id, review request handle, reviewer run id, verdict, findings, and reviewer summary.
- Tests prove a `changes_requested` reviewer artifact with a blocking finding is passed to the Den adapter after reviewer completion.
- Validation command: `python -m pytest -q` → `12 passed`.

This still does not create live Den review findings. It locks in the mapping point where the future real adapter will translate reviewer artifacts into `mcp_den_create_review_finding`, `mcp_den_post_review_findings`, and `mcp_den_set_review_verdict` calls.

Remaining unimplemented pieces now include a real Den MCP client adapter, live Den review-round/finding creation, retry loops, abort/rerun/cleanup controls, and a real Hermes/LLM smoke test.

### delegate_task provider/model override spike

- Unit-test `_resolve_delegation_credentials` with explicit call overrides.
- Unit-test `delegate_task(goal=..., provider=..., model=...)` constructs `AIAgent(provider=..., model=...)` while preserving existing inheritance by default.
- Unit-test ACP override precedence.
- Unit-test invalid provider returns `tool_error` without launching a child.
- Unit-test dynamic schema includes the new fields and safety wording.

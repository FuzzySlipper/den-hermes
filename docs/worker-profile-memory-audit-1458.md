# Worker profile memory audit policy and findings

Task: `den-hermes-bridge` #1458  
Status: policy recorded, gaps enumerated, repeatable audit implemented

## 1. Policy

Tracked spawned-Hermes worker profiles must have **zero medium-term memory** and **zero long-term memory**.

Rationale: Workers are bounded, single-run roles. Their context comes exclusively from the Den task-thread packet, the bounded system prompt, and explicit tool calls during the run. Any persistent memory leaks across runs violate the worker contract and risk cross-task contamination.

### 1.1 Definitions

| Memory class | Examples | Required state |
|-------------|----------|--------------|
| Long-term | `memories/MEMORY.md`, `memories/USER.md`, config `memory.memory_enabled=true`, config `memory.user_profile_enabled=true`, `platform_toolsets` containing `memory` | **Absent or disabled** |
| Medium-term | `state.db`, retained `sessions/`, `checkpoints/` | **Absent or empty** |

### 1.2 Worker profiles in scope

From the runtime registry (`/home/agents/runtime/spawned-hermes-runtimes.yaml`):

| Registry role | Profile name |
|---------------|-------------|
| `coder` | `spawned-coder` |
| `reviewer` | `spawned-reviewer` |
| `validator` | `spawned-validator` |
| `drift_checker` | `spawned-drift-checker` |
| `packet_auditor` | `spawned-packet-auditor` |

Any future worker-style profile prefixed `spawned-` that is registered under a worker role is automatically in scope.

### 1.3 Safe remediation boundary

- **In scope**: config changes, documentation, audit scripts, tests.
- **Out of scope for automated fixes**: deleting live `state.db`, sessions, checkpoints, or memory files from active profile roots. These require explicit operator action or a Hermes-side mechanism to disable memory at startup.
- **Never**: print or copy values from `.env`, `auth.json`, credential pools, or other secret-bearing files during audit.

## 2. Repeatable audit

### 2.1 Command

```bash
python scripts/audit_worker_profile_memory.py
```

JSON mode (for CI or programmatic consumption):

```bash
python scripts/audit_worker_profile_memory.py --json
```

Custom profile root or registry:

```bash
python scripts/audit_worker_profile_memory.py \
  --profile-root /home/agents/profiles \
  --registry /home/agents/runtime/spawned-hermes-runtimes.yaml
```

### 2.2 Exit codes

| Code | Meaning |
|------|---------|
| 0 | All audited worker profiles pass. |
| 1 | One or more worker profiles have memory artifacts or enabled memory. |
| 2 | Audit could not complete (no profiles discovered, unreadable files, missing PyYAML). |

### 2.3 What the audit checks

For each worker profile directory:

1. `memories/MEMORY.md` exists → **long-term finding**
2. `memories/USER.md` exists → **long-term finding**
3. `config.yaml` has `memory.memory_enabled: true` → **long-term finding**
4. `config.yaml` has `memory.user_profile_enabled: true` → **long-term finding**
5. `config.yaml` `platform_toolsets.cli` contains `memory` → **long-term finding**
6. `state.db` exists → **medium-term finding**
7. `sessions/` directory is non-empty → **medium-term finding**
8. `checkpoints/` directory is non-empty → **medium-term finding**

The script parses YAML but does **not** emit raw config values, secret fields, or file contents. It emits only a minimal redacted summary of memory-relevant keys.

### 2.4 Running tests

```bash
python -m pytest tests/test_audit_worker_profile_memory.py -q
```

Tests use temporary fake profiles and registry fixtures. They do not depend on live profile files except when running the optional standalone audit command above.

## 3. Current findings (2026-05-15)

### 3.1 Live audit result

Running the audit against the live profile root and runtime registry yields **FAIL for all five worker profiles**.

Every identified worker profile has:

- `memories/MEMORY.md` (0 bytes)
- `memories/USER.md` (0 bytes)
- `memory.memory_enabled: true`
- `memory.user_profile_enabled: true`
- `platform_toolsets.cli` includes `memory`
- `state.db` present
- `sessions/` directory with retained session files
- `checkpoints/` directory present (for some profiles)

This means **zero compliance** at the time of this writing.

### 3.2 Gap: Hermes does not provide a "fully disable memory" switch

The config fields `memory.memory_enabled` and `memory.user_profile_enabled` are Hermes-level toggles, but setting them to `false` alone may not prevent:

- SQLite `state.db` from being created on first run.
- Session files from being written to `sessions/`.
- Checkpoint snapshots from being stored.

There is no single config key like `worker_mode: true` that atomically disables all persistence. This is an explicit **upstream gap** in Hermes.

### 3.3 Gap: operator action required to purge existing artifacts

Even after config is corrected, the existing `state.db`, `sessions/`, and `checkpoints/` directories in the live profile roots remain. They must be removed by an operator or by a profile reset mechanism. The audit script detects them but does not delete them.

### 3.4 Gap: runtime registry does not enforce memory policy

The runtime registry (`spawned-hermes-runtimes.yaml`) currently has no `memory` stanza for worker roles. The doc #1455 proposes an opt-in `memory.enabled` field, but worker roles should instead explicitly set `memory.enabled: false` (or omit the field, which defaults to false). The registry should be updated to make this explicit.

## 4. Recommended remediation

### 4.1 Config changes (operator, safe)

For each worker profile `config.yaml`, set:

```yaml
memory:
  memory_enabled: false
  user_profile_enabled: false
```

And remove `memory` from `platform_toolsets.cli`.

### 4.2 File cleanup (operator, requires care)

After stopping any running agents for the profile:

```bash
for p in spawned-coder spawned-reviewer spawned-validator spawned-drift-checker spawned-packet-auditor; do
  rm -f /home/agents/profiles/$p/memories/MEMORY.md
  rm -f /home/agents/profiles/$p/memories/USER.md
  rm -f /home/agents/profiles/$p/state.db /home/agents/profiles/$p/state.db-*
  rm -rf /home/agents/profiles/$p/sessions/*
  rm -rf /home/agents/profiles/$p/checkpoints/*
done
```

**Warning**: do not delete `auth.json`, `.env`, or skill state unless explicitly requested.

### 4.3 Registry update

Add an explicit comment or stanza to the runtime registry under each worker role:

```yaml
roles:
  coder:
    # Memory is disabled for all worker profiles per policy #1458.
    # Workers are bounded single-run roles; no persistent memory.
```

### 4.4 Future-proofing: add registry validation

The bridge orchestrator or `runtime_ops` should validate at preflight that worker roles do not have memory enabled. This can be enforced once the registry carries the explicit `memory.enabled: false` field.

## 5. Re-audit schedule

- After any Hermes upgrade that touches memory, session, or checkpoint behavior.
- After any new worker profile is added to the runtime registry.
- As part of the operator preflight checklist before a live spawned-Hermes run.

## 6. Validation checklist

- [x] Audit script exists and is repeatable.
- [x] Tests use temporary fixtures, not live profile state.
- [x] Doc records policy, findings, and remediation guidance.
- [x] No secrets are printed or copied by the audit script.
- [x] Gaps are explicit (Hermes upstream, operator cleanup, registry enforcement).
- [x] Live audit result is documented as failing.

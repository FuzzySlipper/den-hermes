# Den memory provider guinea-pig rollout (#1516)

Task: #1516
Project: `den-hermes-bridge`
Status: initial guinea-pig rollout completed; observation scheduled

## Scope

This rollout enables Den-backed Hermes long-term memory only for the named
non-worker guinea-pig profiles:

| Profile | Read spaces | Write spaces | Default write space |
| --- | --- | --- | --- |
| `researcher` | `assistant:researcher` | `assistant:researcher` | `assistant:researcher` |
| `reviewer` | `assistant:reviewer`, `knowledge_base:den-memory-smoke` | `assistant:reviewer`, `knowledge_base:den-memory-smoke` | `assistant:reviewer` |
| `system-architect` | `assistant:system-architect`, `knowledge_base:den-memory-smoke` | `assistant:system-architect`, `knowledge_base:den-memory-smoke` | `assistant:system-architect` |

No worker/spawned profiles are enabled for Den memory.

## Runtime install path

Den-owned code remains outside upstream Hermes source:

```text
/home/agents/runtime/den-hermes-memory-provider/
├── den/          # Hermes memory provider plugin, symlinked into selected profiles
└── den_hermes/   # Den-owned bridge package used by the plugin
```

Each enabled profile has:

```text
/home/agents/profiles/<profile>/plugins/den -> /home/agents/runtime/den-hermes-memory-provider/den
```

The active Hermes config uses the standard memory-provider selector plus a Den-specific policy block:

```yaml
memory:
  provider: den

den_memory:
  enabled: true
  deny_auto_behavior: true
  project_id: den-hermes-bridge
  profile: <profile>
  read_spaces: [...]
  write_spaces: [...]
  default_write_space: <assistant-space>
  rest:
    base_url: http://192.168.1.10:18080/den-core-api
    timeout_seconds: 10
    retry_attempts: 1
```

`deny_auto_behavior: true` is required by the plugin. Automatic prefetch, turn
sync, session-end extraction, and automatic capture remain no-ops until the
advanced memory super explicitly authorizes them.

## Promotion state

The #1461/#1500 memory provider branch was merged into canonical `main` in the
Den-owned bridge repo worktree, with #1500's Core-facade compatibility code on
the same path as the live guinea-pig install.

The old #1501 Code-Gate submitter blocker is done/resolved by later sysadmin and
den-publish work; it is not a blocker for this rollout.

## Verification bundle

Commands run from `/home/dev/den-hermes-1514-work`:

```bash
PYTHONPATH=/home/agents/hermes-agent:$PWD python -m pytest \
  tests/test_install_den_memory_provider.py \
  tests/test_memory_read_tools.py \
  tests/test_memory_write_tools.py \
  tests/test_memory_service_auth.py \
  tests/test_audit_worker_profile_memory.py -q
# 114 passed

PYTHONPATH=/home/agents/hermes-agent:$PWD python scripts/install_den_memory_provider.py --verify-only --json
# all three guinea-pig profiles status=ok

PYTHONPATH=/home/agents/hermes-agent:$PWD python scripts/audit_worker_profile_memory.py --json
# overall=passed, profiles_audited=5

PYTHONPATH=/home/agents/hermes-agent:$PWD python scripts/smoke_den_memory_profiles_1461.py \
  --live-base-url http://192.168.1.10:18080/den-core-api \
  --json
# status=passed, live_endpoint_probe.status=ok

PYTHONPATH=/home/agents/hermes-agent:$PWD python -m pytest -q
# 263 passed

git diff --check
# passed
```

The live smoke wrote/read the expected solo and shared-KB markers, verified
cross-profile shared-KB recall, verified the researcher shared-KB denial path,
and confirmed profile-local `MEMORY.md` / `USER.md` paths remain distinct.

## Gateway restart

The active gateway services for the three guinea-pig profiles were restarted so
fresh sessions pick up `memory.provider: den` and the `den_memory` config:

```text
hermes-gateway@researcher.service       active/running
hermes-gateway@reviewer.service         active/running
hermes-gateway@system-architect.service active/running
```

Note: these profiles already log a pre-existing `den_channels` platform adapter
configuration warning at gateway startup. That warning is unrelated to the Den
memory provider rollout; the gateway services remain active.

## Observation loop

A shared `memory-curation` skill was added under:

```text
/home/agents/runtime/shared-skills/mcp/memory-curation/SKILL.md
```

Daily dry-run curation wakes were scheduled for seven runs each, with reports
instructed to post to Den task #1516:

| Profile | Cron job |
| --- | --- |
| `researcher` | `den-memory-curation-researcher` |
| `reviewer` | `den-memory-curation-reviewer` |
| `system-architect` | `den-memory-curation-system-architect` |

The curation jobs are dry-run only and must keep automatic capture/prefetch
blocked unless a future task explicitly changes that policy.

# Den memory profile smoke procedure (#1461)

Task: #1461
Parent: #1453 initial opt-in Den memory provider super
Status: smoke/rehearsal procedure plus first execution notes

## Scope and current constraint

This smoke is intentionally **opt-in and named-profile only**. It does not enable Den memory broadly and it does not change spawned worker profiles.

The Hermes-side provider contract from #1457/#1459/#1460 is present in this branch, but the live Den Core memory REST surface expected by the provider is not reachable from this runner at the documented base URL during this smoke:

- expected provider paths: `/api/v1/projects/{project_id}/memory/...`
- tested live base URL in the smoke script: `http://192.168.1.10:5299`
- result at smoke time: provider reports `den_unavailable`

Because of that, the first execution validates the Hermes-side profile/provider behavior with an in-process Den-memory-compatible HTTP server, and records the live endpoint gap explicitly. Do **not** treat the contract smoke as evidence that live Den Core memory entries are available until the live endpoint probe returns `ok`.

## Explicit guinea-pig profiles

The rollout names exactly three existing non-worker Hermes profiles:

| Profile | Purpose | Read spaces | Write spaces | Default write space |
| --- | --- | --- | --- | --- |
| `researcher` | solo assistant-space smoke; no shared KB | `assistant:researcher` | `assistant:researcher` | `assistant:researcher` |
| `reviewer` | shared KB writer/reader | `assistant:reviewer`, `knowledge_base:den-memory-smoke` | `assistant:reviewer`, `knowledge_base:den-memory-smoke` | `assistant:reviewer` |
| `system-architect` | shared KB reader to prove cross-profile retrieval | `assistant:system-architect`, `knowledge_base:den-memory-smoke` | `assistant:system-architect`, `knowledge_base:den-memory-smoke` | `assistant:system-architect` |

This satisfies the required shape:

- at least one profile with its own assistant space and no shared KB (`researcher`);
- at least two profiles sharing a knowledge-base space (`reviewer`, `system-architect`).

## Intended profile config fragment

Only apply this fragment once live Den Core memory REST is available and the operator is comfortable with the named profiles joining the observation window. This key is deliberately separate from Hermes built-in `memory:` (`MEMORY.md` / `USER.md`) so profile-local medium-term memory remains distinct.

```yaml
den_memory:
  enabled: true
  deny_auto_behavior: true
  project_id: den-hermes-bridge
  profile: <profile-name>
  read_spaces:
    - assistant:<profile-name>
    # optional only for shared KB participants:
    - knowledge_base:den-memory-smoke
  write_spaces:
    - assistant:<profile-name>
    # optional only for shared KB participants:
    - knowledge_base:den-memory-smoke
  default_write_space: assistant:<profile-name>
  rest:
    base_url: http://192.168.1.10:5299
    timeout_seconds: 10
    retry_attempts: 1
```

For `researcher`, omit the shared KB entries. For `reviewer` and `system-architect`, include them.

## Smoke command

Run from the bridge checkout that contains the #1457/#1459/#1460 provider code and this #1461 script:

```bash
python scripts/smoke_den_memory_profiles_1461.py --json
```

The script checks:

1. explicit profile list and configured read/write spaces;
2. write to solo assistant space;
3. write to shared KB space from `reviewer`;
4. recall/read shared KB entry from `system-architect`;
5. `researcher` cannot query the shared KB because it is not in `read_spaces`;
6. `den_list_my_memories` returns inspectable entries for a configured shared space;
7. `MEMORY.md`/`USER.md` paths are distinct per named profile;
8. Den-unavailable behavior returns a structured `den_unavailable` status;
9. the live endpoint probe result is reported separately.

Expected contract result while the live endpoint gap remains:

```text
status=passed
contract_smoke.cross_recall_count=1
contract_smoke.solo_shared_space_denied=permission_denied
contract_smoke.den_unavailable_status=den_unavailable
live_endpoint_probe.status=den_unavailable
```

Expected live result after Den Core memory REST is deployed:

```text
live_endpoint_probe.status=ok
```

At that point, rerun a live profile smoke that writes short, clearly tagged `task-1461` memories and then inspect them through `den_list_my_memories` plus Den Desktop / document listing.

## Worker zero-memory check

Worker profiles remain out of scope for Den memory and medium-term memory. Verify with the #1458 audit script after any spawned-worker live run that may recreate profile state:

```bash
python scripts/audit_worker_profile_memory.py --json
```

The current policy requires spawned worker profiles to keep:

- `memory.memory_enabled=false`
- `memory.user_profile_enabled=false`
- `memory.provider=''`
- no Den-memory provider config
- no retained medium-term memory files or state/session/checkpoint artifacts after cleanup

## #1448 observation hook

During the #1453 initial-super observation period, #1448 owns the self-evaluation/curation loop. The curation wake should add a Den long-term memory section for each opted-in guinea-pig profile:

1. call `den_list_my_memories` for the profile's configured spaces;
2. ask the profile to classify each entry as: keep / merge / promote-to-project-doc / promote-to-MEMORY.md / stale / duplicate / squirrel;
3. record proposed actions and token/cost notes;
4. post the evaluation to a Den task/thread for #1448 or a dedicated observation thread.

## First execution notes

The first execution of `scripts/smoke_den_memory_profiles_1461.py --json` passed the Hermes-side contract with the fake server and reported the live endpoint gap separately. The task thread contains the exact JSON summary and the live endpoint status.

## Follow-up gate before broad rollout

Do not broaden this beyond the three named guinea-pig profiles until:

- live Den Core memory REST endpoint probe returns `ok`;
- profile config parsing/wiring registers the explicit tools in actual Hermes sessions, not only the provider class;
- the #1448 curation prompt has a Den-memory self-evaluation section;
- observation notes show low squirrel/stale-memory rates.

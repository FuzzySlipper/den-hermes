# Agent Instance Bindings — Source-of-Truth Relationships

## Overview

Den Hermes bridge has multiple independent systems that track agent
existence, identity, and liveness.  This document defines their
relationships, data flows, and which one to use for each purpose.

## The Four Systems

### 1. Den Channels Memberships

- **What:** Role/agent membership in Den Channels project lanes.
- **API:** `den_channels_get_memberships()`, `den_channels_send_direct_agent_message()`
- **Scope:** Per-channel role-to-agent mapping within a project.
- **Source of truth for:** Which agents are allowed to receive wake
  deliveries, respond to lane messages, and participate in a project.
- **Wake path:** `den_channels_send_direct_agent_message` → Den Gateway
  delivery queue → target profile's Den Channels adapter claim loop.
- **Status:** **Primary wake path.** Green-path delivery (#1624/#1626).
  Does NOT require Core agent_instance_bindings to function.

### 2. Core agent_instance_bindings (`/api/gateway/bindings`)

- **What:** Live-instance projection of running Hermes gateway processes.
- **API:** `mcp_den_list_agent_instance_bindings()`, `/api/gateway/bindings`
- **Fields:** `instance_id`, `project_id`, `agent_identity`, `role`,
  `transport_kind`, `session_id`, `status` (active/degraded/inactive),
  heartbeat timestamp, metadata (profile, machine, scope).
- **Source of truth for:** Which Hermes gateway instances are currently
  running and their last heartbeat (liveness).
- **Wake path:** Used by `DenChannelsWakeBridge._find_bindings()` to
  resolve a target agent/role/project to a specific running gateway
  instance for profile-based wake delivery. **Secondary wake path.**
  Fail-closed: if no binding matches, delivery is rejected.
- **Registration:** Bindings are created via `mcp_den_register_worker_run`
  for spawned-Hermes workers.  Gateway processes do NOT register
  themselves automatically.

### 3. Hermes Gateway Processes (systemd)

- **What:** Actual running `python -m hermes_cli.main --profile <name>
  gateway run --replace` processes.
- **Visibility:** `ps aux | grep "gateway run"`, systemd services.
- **Source of truth for:** Whether a profile's gateway is actually alive
  at the OS level.
- **NOT tracked in:** Core agent_instance_bindings (unless explicitly
  registered, which normal gateway processes are not).

### 4. Legacy Dispatch Rows

- **What:** Retired Den primitive for old push-based dispatch.
- **Status:** **RETIRED.** Do not use for normal queue/wake paths.
- **Label:** Legacy / admin-only.

## Data Flow Diagram

```
                        den_channels_send_direct_agent_message
                                      │
                                      ▼
                          Den Gateway delivery queue
                                      │
                                      ▼
                    Target profile's Den Channels adapter
                    (claims delivery → processes → responds)
                                      │
                      ┌───────────────┴───────────────┐
                      ▼                               ▼
              Channels Memberships          Core agent_instance_bindings
              (who can receive)             (where to route / liveness)
                      │                               │
                      ▼                               ▼
              Direct-agent message           DenChannelsWakeBridge
              (primary wake path)            (secondary wake path)
```

## Current Gap — Updated for Pool-Member Identity

### Profile-Identity vs Worker-Identity duality

As of task #1767, shared spawned-Hermes role profiles (spawned-coder,
spawned-reviewer, spawned-validator, spawned-drift-checker, spawned-packet-auditor)
use **identity duality**:

- **`profile_identity`** (shared display/capability field): the Hermes profile
  name (e.g., `spawned-coder`).  This is the `agent_identity` in Den Channels
  memberships and Core agent_instance_bindings.
- **`worker_identity`** (concrete): the `pool_member_id` that distinguishes one
  pool worker from another within the same role (e.g., `pool-coder-01`).

The Bridge (`DenChannelsWakeBridge`) now supports selecting concrete bindings
from among shared-profile matches when the delivery target carries a
`pool_member_id`, `concrete_identity`, or `agent_instance_id`.  Without a
concrete target, multiple shared-profile bindings still fail closed as
`ambiguous_binding`.

### Agent Binding Matrix

| Agent Identity | Channels Member | Core Binding | Gateway Process | Pool Member ID |
|---|---|---|---|---|
| den-hermes-runner | ✓ | ✓ (canary coder only) | ✓ | (control-plane only) |
| den-mcp-runner | ✓ | ✗ | ✓ | — |
| den-mcp-planner | ✓ | ✗ | ✓ | — |
| den-channels-runner | ✓ | ✗ | ✓ | — |
| den-desktop-runner | ✓ | ✗ | ✓ | — |
| voxelforge-runner | ✓ | ✗ | ✓ | — |
| voxelforge-planner | ✓ | ✗ | ✓ | — |
| spawned-coder | ✓ | ✗ | ✓ | pool-coder-01, pool-coder-02, ... |
| spawned-reviewer | ✓ | ✗ | ✓ | pool-reviewer-01, ... |
| spawned-validator | ✓ | ✗ | ✓ | pool-validator-01, ... |
| spawned-drift-checker | ✓ | ✗ | ✓ | pool-drift-01, ... |
| spawned-packet-auditor | ✓ | ✗ | ✓ | pool-audit-01, ... |
| (all others) | ✓ | ✗ | ✓ | — |

Most active gateway profiles have zero Core bindings.  The primary
Channels wake path works without bindings.  The secondary binding-based
wake path (`DenChannelsWakeBridge`) will fail with `missing_binding`
for every agent except `den-hermes-runner/coder`.

## Recommendation

1. **Primary discovery: Channels memberships.**  Use
   `den_channels_get_memberships()` for resolving who can receive
   messages in a project.  This is the green-path.

2. **Liveness diagnostics: Core bindings + OS check.**  Query
   `mcp_den_list_agent_instance_bindings()` to see tracked instances.
   Supplement with `ps aux | grep "gateway run --profile <name>"` for
   OS-level verification.  A healthy gateway should have both.

3. **Binding registration (auto):** Gateways should self-register on
   startup.  This is a future enhancement for the Hermes gateway or a
   Den plugin hook.  When implemented, the startup lifecycle hook
   calls `mcp_den_register_worker_run(...)` or a new
   `mcp_den_create_agent_instance_binding(...)` with the profile
   identity and project membership.

4. **Binding registration (manual):** Use `register_profile_binding.py`
   (see below) to backfill bindings for currently running gateways.

5. **Legacy dispatch: Do not use.**  Mark any dispatch-only code paths
   as legacy/admin-only.  The `DenChannelsWakeBridge` should not fall
   back to dispatch rows.

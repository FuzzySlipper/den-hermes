# Spawned Role Pool Member Identity Convention

## Purpose

Define how spawned-Hermes role profiles (spawned-coder, spawned-reviewer,
spawned-validator, spawned-drift-checker, spawned-packet-auditor) are
represented as concrete worker-pool members without creating duplicate
Hermes profiles.

## Hard invariant: no duplicate profiles

Role profiles are **shared**.  Every spawned-coder run uses the same
`spawned-coder` Hermes profile — never `spawned-coder-01`,
`spawned-coder-bob`, etc.  Duplicate profiles would violate the central
runtime registry and break the role/source-of-truth contract.

## Identity duality

| Field | Role | Example | Cardinality |
|---|---|---|---|
| `profile_identity` | Shared display/capability field | `spawned-coder` | One per role |
| `worker_identity` | Concrete pool member | `pool-coder-01` | One per instance |
| `agent_instance_id` / `adapter_instance_id` | System-level binding instance | `hermes:den-k8:spawned-coder:wake-a1b2c3` | One per process/lifecycle |

- **`profile_identity`** corresponds to the Hermes profile name in the
  runtime registry.  It is the `agent_identity` in Den Channels memberships
  and Core agent_instance_bindings.
- **`worker_identity`** is the `pool_member_id` that distinguishes one
  pool worker from another within the same role.  It is optional for
  one-shot spawned workers but required for pool members.
- **`agent_instance_id`** is the system-level unique handle for the
  process/lifecycle.  It is the `adapter_instance_id` in Gateway delivery
  metadata and the `instance_id` in Core agent_instance_bindings.

## Delivery targeting

When a delivery targets a shared-profile worker pool:

1. The delivery carries a **`concrete_identity`** field in the target,
   set to either a `pool_member_id` or an `agent_instance_id`.
2. The Bridge uses the concrete identity to select one binding from
   among the shared-profile matches.
3. If the concrete identity matches exactly one active binding, delivery
   proceeds to that instance.
4. If no binding matches the concrete identity, delivery fails with
   a diagnostic identifying the concrete target that could not be found.
5. If the delivery has no concrete identity and multiple bindings match,
   the Bridge fails closed with the existing `ambiguous_binding`
   diagnostic — true ambiguity is never silently resolved.

## `concrete_identity` resolution order

The Bridge resolves a concrete target from the delivery in this order:

1. `target.pool_member_id` — most explicit, preferred for pool-routed deliveries
2. `target.concrete_identity` — generic concrete handle
3. `target.agent_instance_id` — system-level process handle

Matching compares against both the binding's `pool_member_id` / `worker_identity`
and `instance_id` / `adapter_instance_id`. A concrete target must match exactly
one active binding; duplicate concrete binding matches fail closed rather than
choosing the first row.

## Metadata propagation

When the Bridge wakes a worker:

- `DEN_HERMES_POOL_MEMBER_ID` environment variable is set to the
  resolved `pool_member_id` (or empty for one-shot wakes).
- The wake envelope target includes `pool_member_id` and
  `agent_instance_id` fields.
- The envelope also carries `profile_identity` (the shared role profile
  name) and `worker_identity` (the concrete pool member).

## Concrete example

```
Profile:           spawned-coder
Agent identity:    spawned-coder
Pool member ID:    pool-coder-01
Instance ID:       hermes:den-k8:spawned-coder:wake-abc123

Core binding:
  { "instance_id": "hermes:den-k8:spawned-coder:wake-abc123",
    "agent_identity": "spawned-coder",
    "role": "coder",
    "pool_member_id": "pool-coder-01",
    "profile_identity": "spawned-coder",
    "worker_identity": "pool-coder-01",
    "transport_kind": "hermes_profile",
    "status": "active" }

Delivery target:
  { "pool_member_id": "pool-coder-01",
    "agent_identity": "spawned-coder",
    "role": "coder",
    "project_id": "den-hermes-bridge" }

→ Bridge selects the binding where
  pool_member_id == "pool-coder-01" (or instance_id matches)
```

## Upstream contract alignment

- **den-core #1768**: Core lifecycle keyed by `concrete_identity` /
  `pool_member_id`.  `profile_identity` / `worker_role` are shared
  display/capability fields.
- **den-channels #1769/#1771**: Channels carries `agent_instance_id` /
  `pool_member_id`.  `#worker-pool` lobby presence is keyed by
  `concrete_identity = pool_member_id ?? agent_instance_id ?? ''`.
- **den-gateway #1770**: Concrete instance delivery metadata and
  instance-locked claims fail closed without matching concrete evidence.

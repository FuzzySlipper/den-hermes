# Discord / Den Channels profile rollout matrix (#1515)

## Scope

This document records the staged rollout decision for active `hermes-gateway@*.service`
profiles after:

- #1513 made `discord.allow_bots: mentions` effective through profile config.
- #1514 moved the Den Channels Hermes adapter into the Den-owned
  `platforms/den_channels` plugin/overlay path.
- #1509 documented non-interrupting Den Channels internal delivery queueing.

No Discord tokens or secret values were inspected or printed. The inventory below
uses only config values, secret key-name presence, service state, plugin paths, and
Den Channels membership evidence.

## Active gateway profile matrix

| Profile | Service | Class | Discord state | Den Channels plugin | Den Channels membership / binding intent | Rollout decision |
| --- | --- | --- | --- | --- | --- | --- |
| `den-channels-runner` | active/running | Den Channels runner/consumer | no Discord token key detected; config still has safe `allow_bots: mentions`, `auto_thread: false` | enabled; profile plugin path is symlink | active member of `den-channels` project channel as `den-channels-runner` | keep enabled; no Discord rollout needed |
| `den-hermes-runner` | active/running | Discord + Den Channels runner/consumer | Discord token key present; `allow_bots: mentions`, `auto_thread: false` | enabled; profile plugin path is symlink | active member of `den-hermes-bridge` project channel as `den-hermes-runner` | keep enabled; intended dual-surface runner |
| `den-mcp-planner` | active/running | Discord-only planning profile | Discord token key present; `allow_bots: mentions`, `auto_thread: false` | not enabled | no Den Channels membership found in checked project channels | do not enable without explicit Den Channels membership/identity assignment |
| `den-mcp-runner` | active/running | Discord-only runner profile | Discord token key present; `allow_bots: mentions`, `auto_thread: false` | not enabled | no Den Channels membership found in checked project channels | do not enable without explicit Den Channels membership/identity assignment |
| `kate` | active/running | Discord ambassador/bridge candidate | Discord token key present; `allow_bots: mentions`, `auto_thread: false`; `den_channels_ambassador` toolset enabled | `platforms/den_channels` not enabled | ambassador pattern should use tools/Den APIs rather than becoming a Den Channels delivery consumer by default | keep as ambassador; do not enable native consumer plugin by default |
| `researcher` | active/running | Discord-only specialist | Discord token key present; `allow_bots: mentions`, `auto_thread: false` | not enabled | no Den Channels membership found in checked project channels | do not enable without explicit Den Channels membership/identity assignment |
| `reviewer` | active/running | Discord-only specialist | Discord token key present; `allow_bots: mentions`, `auto_thread: false` | not enabled | no Den Channels membership found in checked project channels | do not enable without explicit Den Channels membership/identity assignment |
| `system-architect` | active/running | Discord-only specialist | Discord token key present; `allow_bots: mentions`, `auto_thread: false` | not enabled | no Den Channels membership found in checked project channels | do not enable without explicit Den Channels membership/identity assignment |

## Systemd environment / drop-in check

No active gateway profile had a profile-specific `DISCORD_ALLOW_BOTS` systemd
drop-in. Live process environments also did not expose `DISCORD_ALLOW_BOTS`, which
is acceptable for the current local runtime because #1513 bridges
`discord.allow_bots` from profile config into the Discord adapter policy.

## Den Channels membership evidence

Live Den Channels gateway membership probes through `http://192.168.1.10:18080`:

- `GET /api/gateway/memberships?projectId=den-channels` returned project channel
  `project-den-channels` with active agent member `den-channels-runner` and
  `wakePolicy=all_human_messages`.
- `GET /api/gateway/memberships?projectId=den-hermes-bridge` returned project
  channel `project-den-hermes-bridge` with active agent member
  `den-hermes-runner` and `wakePolicy=all_human_messages`.

No evidence was found that the other Discord-active profiles are intended Den
Channels consumers today.

## Rollout policy

The safe good-state rollout is already present for Discord config across active
Discord-capable profiles:

- `discord.allow_bots: mentions`
- `discord.auto_thread: false`
- no service-level `DISCORD_ALLOW_BOTS` drop-ins needed under the patched runtime

The Den Channels native consumer plugin should **not** be added to every Discord
profile. Enabling `platforms/den_channels` makes a profile eligible to claim Den
Gateway deliveries, so each additional profile needs an explicit intent table:

1. target project/channel membership;
2. `agent_identity`, `role`, `profile`, and `adapter_instance_id`;
3. wake policy and cooldown settings;
4. proof that no other profile will claim the same delivery ambiguously;
5. profile-scoped smoke proving one delivery claim and one visible final reply.

## Staged enablement runbook for any future selected profile

Only after Patch explicitly selects a profile/project/channel:

1. Add/verify Den Channels membership in the target project/channel for the exact
   profile identity.
2. Run from this repo:

   ```bash
   python scripts/install_den_channels_plugin.py --profile <profile>
   python scripts/install_den_channels_plugin.py --verify-only --profile <profile>
   ```

3. Verify plugin discovery with the target profile.
4. Restart only that profile's gateway:

   ```bash
   systemctl --user restart hermes-gateway@<profile>.service
   systemctl --user is-active hermes-gateway@<profile>.service
   ```

5. Run a profile-scoped Den Channels wake/direct-agent smoke and record:
   delivery request id, claimed adapter identity, visible `gateway_delivery` reply
   id, and absence of duplicate cross-profile claims.

## Decision

No additional profiles were changed for #1515. The current state is good:

- Discord bot-to-bot mention config is present in profile config for active
  Discord-capable profiles.
- Den Channels native consumer plugin remains limited to the two profiles with
  matching active Den Channels membership/intent.
- `kate` remains the Discord ambassador/tool bridge rather than a native Den
  Channels delivery consumer.

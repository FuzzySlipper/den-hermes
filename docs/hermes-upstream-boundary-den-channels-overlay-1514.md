# Hermes upstream boundary and Den Channels overlay (#1514)

## Preserved runtime state

Before extraction, the live `/home/agents/hermes-agent` runtime state was preserved at:

- `/home/agents/runtime/hermes-overlays/1514-preserve-20260518T043228Z/`
- bundle: `hermes-agent-current-HEAD.bundle`
- patch series: `patches/0001-...` through `patches/0010-...`
- recorded live head: `882be591df1b6feef6357ebab2e4666c1ecd55ab`
- recorded upstream base: `4c46c35ed0d3864f1cec55d87ab6d0f838ec7a2e`
- checksums: `SHA256SUMS`

The bundle/patches are preservation artifacts, not the desired durable publish target for Den-specific code.

## Lane split

### Lane 1: generic upstreamable Hermes patches

These can be prepared as PR-shaped patches for `NousResearch/hermes-agent`, but Den deployment must not block on upstream push access.

Known PR-shaped patch:

- #1513 / `882be591df1b6feef6357ebab2e4666c1ecd55ab`: `discord.allow_bots` YAML/profile config bridge.
  - Preserved as `patches/0010-fix-bridge-Discord-allow_bots-profile-config.patch`.
  - Touches `gateway/config.py`, `hermes_cli/config.py`, `tests/conftest.py`, and Discord config regression coverage.
  - This remains a local overlay dependency for live Kate/multi-agent Discord until upstream/current runtime includes it.

Additional generic patch candidates from the current local runtime overlay:

- #1510 compaction active-turn preservation:
  - `patches/0006-Fix-compaction-active-request-preservation.patch`
  - `patches/0008-Preserve-active-turn-suffix-during-compaction.patch`
  - These touch `agent/context_compressor.py` and focused tests. They are generic Hermes behavior, not Den Channels product code.

Potentially upstreamable gateway hooks, but still motivated by Den Channels and should be split/reviewed carefully before any upstream PR:

- `patches/0007-fix-preserve-Den-delivery-metadata-on-final-sends.patch`
  - touches generic `gateway/platforms/base.py` / `gateway/run.py` send metadata propagation.
  - The generic shape is “preserve adapter-supplied delivery metadata through final sends,” while Den-specific semantics stay in the plugin.

### Lane 2: Den-owned plugin/overlay

The Den Channels platform adapter is now copied into this Den-owned repo under:

- `plugins/platforms/den_channels/`

Install it into profile/user plugin space using:

```bash
python scripts/install_den_channels_plugin.py \
  --profile den-channels-runner \
  --profile den-hermes-runner
```

Default install behavior:

1. Copies this repo's `plugins/platforms/den_channels` source into the shared Den-owned runtime root:
   - `/home/agents/runtime/den-hermes-plugins/platforms/den_channels`
2. Symlinks each profile's user plugin path to that shared plugin:
   - `/home/agents/profiles/<profile>/plugins/platforms/den_channels`
3. Ensures each profile config includes `plugins.enabled: ["platforms/den_channels"]` (or the equivalent manifest name).

Verification-only mode:

```bash
python scripts/install_den_channels_plugin.py \
  --verify-only \
  --profile den-channels-runner \
  --profile den-hermes-runner
```

This uses Hermes' normal user-plugin discovery path, so a clean upstream checkout plus the profile plugin symlink can discover the Den Channels adapter without adding Den-owned source files back into upstream Hermes.

## Temporary local overlay patches

Until Hermes upstream/current runtime includes the generic pieces, operators may need to apply local overlay patches after a `hermes update` or clean checkout. Use the preserved patch series, but apply only the required lane:

- For #1513 `discord.allow_bots`: apply `0010-fix-bridge-Discord-allow_bots-profile-config.patch`.
- For #1510 compaction behavior if still required: apply `0006` and `0008` after reviewing current upstream context.
- Do **not** apply the Den Channels adapter patches (`0001`-`0005`, `0009`) as upstream Hermes source edits in the durable path; install `plugins/platforms/den_channels` from this repo instead.

Example overlay application against a clean Hermes checkout:

```bash
cd /path/to/hermes-agent
# Generic config bridge only:
git am /home/agents/runtime/hermes-overlays/1514-preserve-20260518T043228Z/patches/0010-fix-bridge-Discord-allow_bots-profile-config.patch
```

## Validation checklist

From a clean or simulated-clean Hermes checkout:

1. Apply required generic overlay patches that are not yet in upstream/current runtime.
2. Run the Den-owned plugin install script for relevant profiles.
3. Verify plugin discovery:

   ```bash
   HERMES_HOME=/home/agents/profiles/den-channels-runner hermes plugins list | grep -E 'den-channels|platforms/den_channels'
   HERMES_HOME=/home/agents/profiles/den-hermes-runner hermes plugins list | grep -E 'den-channels|platforms/den_channels'
   ```

4. Verify profile plugin/config state:

   ```bash
   python /home/dev/den-hermes/scripts/install_den_channels_plugin.py --verify-only \
     --profile den-channels-runner --profile den-hermes-runner
   ```

5. Restart gateway services only after the plugin and any generic overlay patches are present:

   ```bash
   systemctl --user restart hermes-gateway@den-channels-runner.service
   systemctl --user restart hermes-gateway@den-hermes-runner.service
   ```

6. Check logs for `den_channels connected` and run a Den Channels direct-agent/channel wake smoke. The task is not fully restorable until a wake can be claimed and answered or a concrete remaining blocker is recorded.

## Operator rule

Lack of push access to `NousResearch/hermes-agent` is not a blocker for Den-specific Channels/Gateway behavior. Treat upstream push failure as an ownership-boundary signal: generic fixes can be PR-shaped; Den product integration belongs here or in a Den-owned shared plugin path.

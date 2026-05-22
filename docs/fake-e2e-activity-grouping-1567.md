# Fake E2E spawned-worker activity grouping (#1567)

This repository covers Den spawned-worker activity grouping without live Den services or LLM calls through deterministic fake Hermes subprocesses and in-process Den Channels adapter tests.

## Commands

Run the focused fake E2E and activity-shape checks from the repo root:

```bash
PYTHONPATH=/home/agent/.hermes/hermes-agent uv run --with pytest --with pytest-asyncio python -m pytest -q \
  tests/test_den_channels_adapter_queue_context.py \
  tests/test_orchestrator_fake_e2e.py

git diff --check
```

## Canonical fixture IDs

The #1567 fake coverage uses aligned IDs across worker launch and activity payload assertions:

- parent display block: `parent-1567`
- coder worker run: `coder-1567`
- reviewer worker run: `reviewer-1567`

## Activity payload invariant

Spawned workers inherit the parent `DEN_CHANNELS_ACTIVITY_CONTEXT`, but emit tool activity as child profile identities. The canonical camelCase shape intentionally uses `displayBlockId` (not `displayDeliveryRequestId`) and forwards these fields to Den Channels/Gateway activity consumers:

```json
{
  "agentIdentity": "den-coder-profile",
  "deliveryRequestId": "701",
  "displayBlockId": "parent-1567",
  "parentHermesSessionKey": "parent-session-1567",
  "parentAgentIdentity": "den-mcp-runner",
  "workerRunId": "coder-1567",
  "workerRole": "coder",
  "metadataJson": "{\"workerRunId\":\"coder-1567\",\"workerRole\":\"coder\"}"
}
```

The same shape is expected for reviewer activity with `agentIdentity=den-reviewer-profile`, `workerRunId=reviewer-1567`, and `workerRole=reviewer`.

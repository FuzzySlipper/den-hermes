"""Bounded local gopher/courier agent — delivery babysitting prototype.

This module defines a deterministic FSM/executor that sits outside the LLM
and enforces strict schema validation on any LLM-produced action proposals.
The gopher monitors delivery evidence (from Gateway wake events and
callback status routes) and can perform a limited set of actions:

- ack_sender:  acknowledge receipt of a delivery wake
- wait:        skip this tick, check again after next_check_seconds
- nudge_target: gentle reminder to the target agent
- notify_human: escalate to human operator
- record_observation: log an evidence observation without posting
- no_op:       explicitly do nothing

Design invariants:
- The FSM is deterministic and fakeable: no real I/O in constructor.
- Invalid/drifted model output => fail-closed (record_observation / notify_human).
- No recursive self-wake cycles: wake messages cannot target 'gopher' or 'courier'.
- Dedupe keys prevent repeated stuck checks from spamming.
- The prototype has NO production posting privileges unless explicitly
  running in dry-run/evidence-only mode.
"""

from __future__ import annotations

import enum
import re
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_MESSAGE_LENGTH = 2000
MAX_NUDGE_COUNT = 3
MAX_NOTIFICATION_COUNT = 2
MAX_NEXT_CHECK_SECONDS = 3600  # 1 hour
MIN_NEXT_CHECK_SECONDS = 5
SELF_TARGET_PATTERNS = re.compile(r"^gopher|^courier", re.IGNORECASE)

CANONICAL_ACTIONS = frozenset({
    "ack_sender",
    "wait",
    "nudge_target",
    "notify_human",
    "record_observation",
    "no_op",
})

CANONICAL_REASONS = frozenset({
    "recorded",
    "unclaimed",
    "claimed_no_activity",
    "provider_slow",
    "tool_waiting",
    "suppressed",
    "target_offline",
    "unknown",
    "callback_persisted",
})


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class GopherAction(str, enum.Enum):
    """Allowed gopher actions, ordered by escalation level."""
    ACK_SENDER = "ack_sender"
    WAIT = "wait"
    NUDGE_TARGET = "nudge_target"
    NOTIFY_HUMAN = "notify_human"
    RECORD_OBSERVATION = "record_observation"
    NO_OP = "no_op"

    def escalation_level(self) -> int:
        """Higher = more intrusive/urgent. Used by FSM for escalation gating."""
        return {
            GopherAction.NO_OP: 0,
            GopherAction.WAIT: 1,
            GopherAction.RECORD_OBSERVATION: 2,
            GopherAction.ACK_SENDER: 3,
            GopherAction.NUDGE_TARGET: 4,
            GopherAction.NOTIFY_HUMAN: 5,
        }[self]


class GopherReason(str, enum.Enum):
    """Reasons justifying the chosen action."""
    RECORDED = "recorded"
    UNCLAIMED = "unclaimed"
    CLAIMED_NO_ACTIVITY = "claimed_no_activity"
    PROVIDER_SLOW = "provider_slow"
    TOOL_WAITING = "tool_waiting"
    SUPPRESSED = "suppressed"
    TARGET_OFFLINE = "target_offline"
    UNKNOWN = "unknown"
    CALLBACK_PERSISTED = "callback_persisted"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeliveryEvidence:
    """Typed evidence input model from Gateway wake / callback events."""
    message_id: str
    delivery_id: str
    target_agent: str
    channel_id: str
    status: str
    gateway_span_ms: float | None
    bridge_span_ms: float | None
    provider_timing_unavailable: bool
    waterfall_labels: tuple[str, ...] = ()
    existing_actions: tuple[dict[str, Any], ...] = ()
    timestamp_epoch: float = 0.0
    age_seconds: float = 0.0

    @property
    def dedupe_key(self) -> str:
        """Unique key for this deliverable to prevent repeated-action spam."""
        return f"d:{self.delivery_id}"

    @property
    def is_callback_persisted(self) -> bool:
        return self.status == "callback_persisted"

    @property
    def is_unclaimed(self) -> bool:
        return self.status == "unclaimed"

    @property
    def is_provider_slow(self) -> bool:
        return self.provider_timing_unavailable or (
            self.gateway_span_ms is not None and self.gateway_span_ms > 500
        )


@dataclass(frozen=True)
class ModelActionProposal:
    """Parsed and validated output from an LLM model call."""
    action: GopherAction
    reason: GopherReason
    target_agent: str
    channel_id: str
    message: str = ""
    next_check_seconds: int = 60
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IncidentDedupeRecord:
    """Tracks a dedupe-able incident to prevent spam."""
    dedupe_key: str
    action: GopherAction
    reason: GopherReason
    timestamp: float
    count: int = 1


@dataclass(frozen=True)
class EvidencePacket:
    """Complete evidence packet produced by a gopher tick."""
    delivery_evidence: DeliveryEvidence
    model_proposal: ModelActionProposal | None
    fsm_action: GopherAction
    fsm_reason: GopherReason
    schema_valid: bool
    dedupe_suppressed: bool
    dedupe_count: int
    validation_errors: tuple[str, ...] = ()
    model_raw_output: str = ""


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def validate_model_json_output(
    raw: dict[str, Any],
    evidence: DeliveryEvidence,
    dedupe_records: Mapping[str, IncidentDedupeRecord],
    nudge_count: int = 0,
    notify_count: int = 0,
) -> tuple[ModelActionProposal | None, list[str]]:
    """Validate an LLM-produced JSON dict against the action schema.

    Returns (parsed_proposal_or_None, list_of_errors).
    If any validation fails, proposal is None (fail-closed).
    """
    errors: list[str] = []

    # --- Required fields ---
    action_str = raw.get("action")
    reason_str = raw.get("reason")
    target_agent = raw.get("target_agent", "")
    channel_id = raw.get("channel_id", "")

    if not action_str or not isinstance(action_str, str):
        errors.append("Missing or non-string 'action'")
    if not reason_str or not isinstance(reason_str, str):
        errors.append("Missing or non-string 'reason'")
    if not target_agent or not isinstance(target_agent, str):
        errors.append("Missing or non-string 'target_agent'")
    if not channel_id or not isinstance(channel_id, str):
        errors.append("Missing or non-string 'channel_id'")

    if errors:
        return None, errors

    # --- Enum validation ---
    try:
        action = GopherAction(action_str)
    except ValueError:
        errors.append(f"Invalid action '{action_str}'. Must be one of: {sorted(CANONICAL_ACTIONS)}")
        return None, errors

    try:
        reason = GopherReason(reason_str)
    except ValueError:
        errors.append(f"Invalid reason '{reason_str}'. Must be one of: {sorted(CANONICAL_REASONS)}")
        return None, errors

    # --- Self-recursive wake guard ---
    if SELF_TARGET_PATTERNS.match(target_agent):
        errors.append(
            f"Self-recursive wake guard triggered: target_agent '{target_agent}' "
            f"matches gopher/courier pattern. Fail-closed."
        )
        return None, errors

    # --- Target / channel match against evidence ---
    if target_agent != evidence.target_agent:
        errors.append(
            f"Target agent mismatch: proposal says '{target_agent}', "
            f"evidence says '{evidence.target_agent}'"
        )
    if channel_id != evidence.channel_id:
        errors.append(
            f"Channel ID mismatch: proposal says '{channel_id}', "
            f"evidence says '{evidence.channel_id}'"
        )

    if errors:
        return None, errors

    # --- Message validation ---
    message = raw.get("message", "")
    if not isinstance(message, str):
        errors.append("'message' must be a string")
        return None, errors
    if len(message) > MAX_MESSAGE_LENGTH:
        errors.append(f"Message too long: {len(message)} > {MAX_MESSAGE_LENGTH}")

    if errors:
        return None, errors

    # --- next_check_seconds clamp ---
    ncs = raw.get("next_check_seconds", 60)
    if not isinstance(ncs, int) or ncs < MIN_NEXT_CHECK_SECONDS:
        ncs = MIN_NEXT_CHECK_SECONDS
    if ncs > MAX_NEXT_CHECK_SECONDS:
        ncs = MAX_NEXT_CHECK_SECONDS

    # --- Budget checks for nudge/notify ---
    if action == GopherAction.NUDGE_TARGET and nudge_count >= MAX_NUDGE_COUNT:
        errors.append(
            f"Nudge budget exhausted: {nudge_count} >= {MAX_NUDGE_COUNT}"
        )
        return None, errors

    if action == GopherAction.NOTIFY_HUMAN and notify_count >= MAX_NOTIFICATION_COUNT:
        errors.append(
            f"Notification budget exhausted: {notify_count} >= {MAX_NOTIFICATION_COUNT}"
        )
        return None, errors

    # --- Dedupe suppression ---
    dedupe_key = evidence.dedupe_key
    dedup_record = dedupe_records.get(dedupe_key)
    if dedup_record and dedup_record.action == action:
        # Same action on same delivery: suppress if within recent window
        age = time.time() - dedup_record.timestamp
        if age < 300 and dedup_record.count >= 2:
            errors.append(
                f"Dedupe suppression: {dedup_record.action} already recorded "
                f"{dedup_record.count}x for {dedupe_key}, last {age:.0f}s ago"
            )
            return None, errors

    payload = raw.get("payload", {})
    if not isinstance(payload, dict):
        payload = {}

    return ModelActionProposal(
        action=action,
        reason=reason,
        target_agent=target_agent,
        channel_id=channel_id,
        message=message,
        next_check_seconds=ncs,
        payload=payload,
    ), errors


# ---------------------------------------------------------------------------
# Deterministic FSM
# ---------------------------------------------------------------------------


def select_action(
    evidence: DeliveryEvidence,
    proposal: ModelActionProposal | None,
    proposal_errors: Sequence[str],
    dedupe_records: Mapping[str, IncidentDedupeRecord],
    nudge_count: int = 0,
    notify_count: int = 0,
) -> tuple[GopherAction, GopherReason]:
    """Deterministic FSM that selects the final action and reason.

    Rules (checked in order):
    1. If model output is invalid/fail-closed => record_observation (or notify_human)
       if the delivery evidence indicates urgency/stuck.
    2. If dedupe suppressed => no_op.
    3. If callback_persisted with no issues => no_op (delivery complete).
    4. If fresh unclaimed receipt => ack_sender, observe progress.
    5. If stuck (claimed_no_activity, provider_slow, target_offline):
       - First stuck: nudge_target
       - Second+ stuck: notify_human (escalate)
       - Budget exhausted: record_observation
    6. Otherwise use model proposal as-is.
    """
    # Rule 1: Invalid model / fail-closed
    if proposal is None:
        # Check for urgent stuck state that warrants human notification
        if evidence.is_callback_persisted:
            return GopherAction.NO_OP, GopherReason.CALLBACK_PERSISTED
        if _is_stuck(evidence):
            if notify_count < MAX_NOTIFICATION_COUNT:
                return GopherAction.NOTIFY_HUMAN, GopherReason.TARGET_OFFLINE
            return GopherAction.RECORD_OBSERVATION, GopherReason.UNKNOWN
        return GopherAction.RECORD_OBSERVATION, GopherReason.UNKNOWN

    dedupe_key = evidence.dedupe_key
    dedup_record = dedupe_records.get(dedupe_key)

    # Rule 2: Dedupe suppressed
    if dedup_record and dedup_record.action == proposal.action:
        age = time.time() - dedup_record.timestamp
        if age < 300 and dedup_record.count >= 2:
            return GopherAction.NO_OP, GopherReason.SUPPRESSED

    # Rule 3: Already persisted => no action needed
    if evidence.is_callback_persisted:
        return GopherAction.NO_OP, GopherReason.CALLBACK_PERSISTED

    # Rule 4: Fresh unclaimed receipt => ack_sender, observe
    if evidence.is_unclaimed and proposal.action == GopherAction.ACK_SENDER:
        return GopherAction.ACK_SENDER, GopherReason.RECORDED

    # Rule 5: Stuck incident handling
    if _is_stuck(evidence):
        if nudge_count < MAX_NUDGE_COUNT:
            return GopherAction.NUDGE_TARGET, GopherReason.CLAIMED_NO_ACTIVITY
        if notify_count < MAX_NOTIFICATION_COUNT:
            return GopherAction.NOTIFY_HUMAN, GopherReason.TARGET_OFFLINE
        return GopherAction.RECORD_OBSERVATION, GopherReason.CLAIMED_NO_ACTIVITY

    # Rule 6: Accept model proposal if reasonable
    # But constrain escalation — if model proposes higher than FSM allows,
    # cap at the appropriate level
    if proposal.action in (GopherAction.NUDGE_TARGET, GopherAction.NOTIFY_HUMAN):
        if not _is_stuck(evidence):
            return GopherAction.WAIT, GopherReason.RECORDED

    return proposal.action, proposal.reason


def _is_stuck(evidence: DeliveryEvidence) -> bool:
    """Heuristic: is this delivery stuck and needs intervention?"""
    if evidence.is_callback_persisted:
        return False
    if evidence.is_unclaimed:
        return False
    if evidence.status in ("claimed_no_activity", "target_offline"):
        return True
    if evidence.is_provider_slow:
        return True
    return evidence.age_seconds > 600  # 10 minutes stale


# ---------------------------------------------------------------------------
# Dedupe tracker
# ---------------------------------------------------------------------------


def update_dedupe(
    records: dict[str, IncidentDedupeRecord],
    evidence: DeliveryEvidence,
    action: GopherAction,
    reason: GopherReason,
) -> dict[str, IncidentDedupeRecord]:
    """Update dedupe records for the given evidence+action pair.

    Returns new records dict (immutable-style update, but mutates in place for
    performance — caller owns the dict).
    """
    dedupe_key = evidence.dedupe_key
    now = time.time()

    existing = records.get(dedupe_key)
    if existing and existing.action == action:
        records[dedupe_key] = IncidentDedupeRecord(
            dedupe_key=dedupe_key,
            action=action,
            reason=reason,
            timestamp=now,
            count=existing.count + 1,
        )
    else:
        records[dedupe_key] = IncidentDedupeRecord(
            dedupe_key=dedupe_key,
            action=action,
            reason=reason,
            timestamp=now,
            count=1,
        )
    return records


# ---------------------------------------------------------------------------
# Evidence packet builder
# ---------------------------------------------------------------------------


def build_evidence_packet(
    evidence: DeliveryEvidence,
    model_proposal: ModelActionProposal | None,
    fsm_action: GopherAction,
    fsm_reason: GopherReason,
    schema_valid: bool,
    dedupe_suppressed: bool,
    dedupe_count: int,
    validation_errors: Sequence[str] = (),
    model_raw_output: str = "",
) -> EvidencePacket:
    """Build a complete evidence packet from a gopher tick."""
    return EvidencePacket(
        delivery_evidence=evidence,
        model_proposal=model_proposal,
        fsm_action=fsm_action,
        fsm_reason=fsm_reason,
        schema_valid=schema_valid,
        dedupe_suppressed=dedupe_suppressed,
        dedupe_count=dedupe_count,
        validation_errors=tuple(validation_errors),
        model_raw_output=model_raw_output,
    )


# ---------------------------------------------------------------------------
# High-level tick: one cycle of the gopher loop
# ---------------------------------------------------------------------------


def run_gopher_tick(
    evidence: DeliveryEvidence,
    model_raw_json: dict[str, Any] | None,
    dedupe_records: dict[str, IncidentDedupeRecord] | None = None,
    nudge_count: int = 0,
    notify_count: int = 0,
) -> EvidencePacket:
    """Execute one deterministic gopher tick.

    This is the main entry point for the prototype harness. In production
    this would be called in a loop; for now it's a single-shot tick for
    testing and evaluation.

    Args:
        evidence: Delivery evidence from Gateway/callback.
        model_raw_json: Raw JSON dict from LLM (or None for offline/fake mode).
        dedupe_records: Mutable dict of dedupe records (updated in place).
        nudge_count: Number of nudges already sent for this delivery.
        notify_count: Number of human notifications already sent.

    Returns:
        EvidencePacket with all decisions, validation results, and metadata.
    """
    if dedupe_records is None:
        dedupe_records = {}

    # Phase 1: Validate model output
    proposal: ModelActionProposal | None = None
    val_errors: list[str] = []
    schema_valid = False

    if model_raw_json is not None:
        proposal, val_errors = validate_model_json_output(
            raw=model_raw_json,
            evidence=evidence,
            dedupe_records=dedupe_records,
            nudge_count=nudge_count,
            notify_count=notify_count,
        )
        schema_valid = proposal is not None

    # Phase 2: FSM selects final action
    fsm_action, fsm_reason = select_action(
        evidence=evidence,
        proposal=proposal,
        proposal_errors=val_errors,
        dedupe_records=dedupe_records,
        nudge_count=nudge_count,
        notify_count=notify_count,
    )

    # Phase 3: Check dedupe
    dedupe_key = evidence.dedupe_key
    dedup_record = dedupe_records.get(dedupe_key)
    dedupe_suppressed = False
    dedupe_count = 0

    if dedup_record and dedup_record.action == fsm_action:
        age = time.time() - dedup_record.timestamp
        dedupe_count = dedup_record.count
        if age < 300 and dedup_record.count >= 2:
            fsm_action = GopherAction.NO_OP
            fsm_reason = GopherReason.SUPPRESSED
            dedupe_suppressed = True

    # Update dedupe records
    update_dedupe(dedupe_records, evidence, fsm_action, fsm_reason)

    # Phase 4: Build packet
    model_raw_str = ""
    if model_raw_json is not None:
        import json
        model_raw_str = json.dumps(model_raw_json, default=str)

    return build_evidence_packet(
        evidence=evidence,
        model_proposal=proposal,
        fsm_action=fsm_action,
        fsm_reason=fsm_reason,
        schema_valid=schema_valid,
        dedupe_suppressed=dedupe_suppressed,
        dedupe_count=dedupe_count,
        validation_errors=val_errors,
        model_raw_output=model_raw_str,
    )

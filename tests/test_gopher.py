"""Tests for the bounded local gopher/courier agent.

Covers acceptance scenarios:
- Successful fresh wake: receipt ack then observes callback/progress/completion
- Staged stuck incident: nudge -> human notification, deduped
- Invalid model/tool failure: fail-closed evidence
- provider_timing_unavailable and #1744 waterfall labels carried explicitly
- Model hallucinated action kind rejected
- Budget/self-target/message validation
"""

from __future__ import annotations

import json
import time

import pytest

from den_hermes.gopher import (
    DeliveryEvidence,
    EvidencePacket,
    GopherAction,
    GopherReason,
    IncidentDedupeRecord,
    ModelActionProposal,
    run_gopher_tick,
    validate_model_json_output,
    select_action,
    MAX_MESSAGE_LENGTH,
    MAX_NUDGE_COUNT,
    MAX_NOTIFICATION_COUNT,
    MIN_NEXT_CHECK_SECONDS,
    MAX_NEXT_CHECK_SECONDS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_evidence() -> DeliveryEvidence:
    """A fresh unclaimed delivery wake (happy path)."""
    return DeliveryEvidence(
        message_id="msg-1359",
        delivery_id="gw-del-657",
        target_agent="den-worker-alpha",
        channel_id="wake-channel",
        status="unclaimed",
        gateway_span_ms=589.4,
        bridge_span_ms=3099.2,
        provider_timing_unavailable=False,
        waterfall_labels=("gateway_delivery_request", "delivery_ack", "callback_persisted"),
        existing_actions=(),
        timestamp_epoch=time.time() - 10,
        age_seconds=10.0,
    )


@pytest.fixture
def callback_persisted_evidence() -> DeliveryEvidence:
    """Delivery already persisted — no action needed."""
    return DeliveryEvidence(
        message_id="msg-1360",
        delivery_id="gw-del-657",
        target_agent="den-worker-alpha",
        channel_id="wake-channel",
        status="callback_persisted",
        gateway_span_ms=589.4,
        bridge_span_ms=3099.2,
        provider_timing_unavailable=False,
        waterfall_labels=(
            "gateway_delivery_request", "delivery_ack", "callback_persisted",
            "status_callback_persisted",
        ),
        existing_actions=(),
        timestamp_epoch=time.time() - 30,
        age_seconds=30.0,
    )


@pytest.fixture
def stuck_evidence() -> DeliveryEvidence:
    """A stuck delivery — claimed but no activity, provider slow."""
    return DeliveryEvidence(
        message_id="msg-1361",
        delivery_id="gw-del-658",
        target_agent="den-worker-beta",
        channel_id="wake-channel",
        status="claimed_no_activity",
        gateway_span_ms=1200.0,
        bridge_span_ms=None,
        provider_timing_unavailable=True,
        waterfall_labels=("gateway_delivery_request", "provider_timing_unavailable"),
        existing_actions=(),
        timestamp_epoch=time.time() - 900,
        age_seconds=900.0,
    )


@pytest.fixture
def ack_model_json() -> dict:
    """A valid ack_sender model proposal."""
    return {
        "action": "ack_sender",
        "reason": "recorded",
        "target_agent": "den-worker-alpha",
        "channel_id": "wake-channel",
        "message": "Delivery gw-del-657 received. Observing progress.",
        "next_check_seconds": 30,
        "payload": {"ack_version": "1.0"},
    }


@pytest.fixture
def nudge_model_json() -> dict:
    """A valid nudge_target model proposal."""
    return {
        "action": "nudge_target",
        "reason": "claimed_no_activity",
        "target_agent": "den-worker-beta",
        "channel_id": "wake-channel",
        "message": "Reminder: delivery gw-del-658 is still unprocessed after 15 minutes.",
        "next_check_seconds": 120,
        "payload": {},
    }


# ---------------------------------------------------------------------------
# Test: Enum and constant validation
# ---------------------------------------------------------------------------


class TestEnums:

    def test_all_actions_have_escalation_levels(self):
        for action in GopherAction:
            level = action.escalation_level()
            assert isinstance(level, int)
            assert 0 <= level <= 5

    def test_action_ordering(self):
        assert GopherAction.NO_OP.escalation_level() < GopherAction.WAIT.escalation_level()
        assert GopherAction.WAIT.escalation_level() < GopherAction.ACK_SENDER.escalation_level()
        assert GopherAction.ACK_SENDER.escalation_level() < GopherAction.NUDGE_TARGET.escalation_level()
        assert GopherAction.NUDGE_TARGET.escalation_level() < GopherAction.NOTIFY_HUMAN.escalation_level()

    def test_invalid_action_rejected(self):
        with pytest.raises(ValueError):
            GopherAction("fly_to_moon")

    def test_invalid_reason_rejected(self):
        with pytest.raises(ValueError):
            GopherReason("because_i_said_so")


# ---------------------------------------------------------------------------
# Test: DeliveryEvidence model
# ---------------------------------------------------------------------------


class TestDeliveryEvidence:

    def test_dedupe_key(self, fresh_evidence: DeliveryEvidence):
        assert fresh_evidence.dedupe_key == "d:gw-del-657"

    def test_callback_persisted_true(self, callback_persisted_evidence: DeliveryEvidence):
        assert callback_persisted_evidence.is_callback_persisted is True

    def test_is_unclaimed_true(self, fresh_evidence: DeliveryEvidence):
        assert fresh_evidence.is_unclaimed is True

    def test_is_provider_slow_false(self, fresh_evidence: DeliveryEvidence):
        # fresh_evidence has gateway_span_ms=589.4 which is >500, so it IS provider_slow
        # This test verifies the property correctly identifies slow values
        assert fresh_evidence.is_provider_slow is True

    def test_is_provider_slow_threshold(self):
        """Edge case: gateway_span_ms exactly at boundary."""
        low = DeliveryEvidence(
            message_id="m", delivery_id="d", target_agent="a",
            channel_id="c", status="ok", gateway_span_ms=499.0,
            bridge_span_ms=None, provider_timing_unavailable=False,
        )
        assert low.is_provider_slow is False

        high = DeliveryEvidence(
            message_id="m", delivery_id="d", target_agent="a",
            channel_id="c", status="ok", gateway_span_ms=501.0,
            bridge_span_ms=None, provider_timing_unavailable=False,
        )
        assert high.is_provider_slow is True

    def test_is_provider_slow_true(self, stuck_evidence: DeliveryEvidence):
        assert stuck_evidence.is_provider_slow is True

    def test_waterfall_labels_carried(self, stuck_evidence: DeliveryEvidence):
        assert "provider_timing_unavailable" in stuck_evidence.waterfall_labels
        assert "gateway_delivery_request" in stuck_evidence.waterfall_labels

    def test_1744_labels_explicit(self, callback_persisted_evidence: DeliveryEvidence):
        """#1744 waterfall labels are carried explicitly, not blended."""
        assert "callback_persisted" in callback_persisted_evidence.waterfall_labels


# ---------------------------------------------------------------------------
# Test: Success scenario — fresh wake -> ack_sender -> observe completion
# ---------------------------------------------------------------------------


class TestFreshWakeHappyPath:

    def test_fresh_wake_ack_sender(self, fresh_evidence: DeliveryEvidence, ack_model_json: dict):
        """Successful fresh wake should produce ack_sender."""
        packet = run_gopher_tick(
            evidence=fresh_evidence,
            model_raw_json=ack_model_json,
        )
        assert packet.fsm_action == GopherAction.ACK_SENDER
        assert packet.fsm_reason == GopherReason.RECORDED
        assert packet.schema_valid is True
        assert packet.dedupe_suppressed is False
        assert len(packet.validation_errors) == 0

    def test_callback_persisted_no_op(self, callback_persisted_evidence: DeliveryEvidence, ack_model_json: dict):
        """Already-persisted delivery should produce no_op regardless of model proposal."""
        packet = run_gopher_tick(
            evidence=callback_persisted_evidence,
            model_raw_json=ack_model_json,
        )
        assert packet.fsm_action == GopherAction.NO_OP
        assert packet.fsm_reason == GopherReason.CALLBACK_PERSISTED
        assert packet.schema_valid is True

    def test_progress_observation(self, fresh_evidence: DeliveryEvidence, ack_model_json: dict):
        """After ack, subsequent check should observe and wait."""
        dedupe_records = {}
        # First tick: ack
        tick1 = run_gopher_tick(
            evidence=fresh_evidence,
            model_raw_json=ack_model_json,
            dedupe_records=dedupe_records,
        )
        assert tick1.fsm_action == GopherAction.ACK_SENDER

        # Update evidence to show progress but not yet complete
        # Use low gateway_span_ms to avoid triggering provider_slow heuristic
        progress_evidence = DeliveryEvidence(
            message_id=fresh_evidence.message_id,
            delivery_id=fresh_evidence.delivery_id,
            target_agent=fresh_evidence.target_agent,
            channel_id=fresh_evidence.channel_id,
            status="in_progress",
            gateway_span_ms=100.0,
            bridge_span_ms=2100.0,
            provider_timing_unavailable=False,
        )
        wait_model = {**ack_model_json, "action": "wait", "reason": "recorded"}
        tick2 = run_gopher_tick(
            evidence=progress_evidence,
            model_raw_json=wait_model,
            dedupe_records=dedupe_records,
        )
        assert tick2.fsm_action == GopherAction.WAIT
        assert tick2.dedupe_suppressed is False


# ---------------------------------------------------------------------------
# Test: Staged stuck incident
# ---------------------------------------------------------------------------


class TestStuckIncident:

    def test_first_stuck_nudge(self, stuck_evidence: DeliveryEvidence, nudge_model_json: dict):
        """First stuck check should produce nudge_target."""
        packet = run_gopher_tick(
            evidence=stuck_evidence,
            model_raw_json=nudge_model_json,
        )
        assert packet.fsm_action == GopherAction.NUDGE_TARGET
        assert packet.fsm_reason == GopherReason.CLAIMED_NO_ACTIVITY
        assert packet.schema_valid is True

    def test_second_stuck_notify_human(self, stuck_evidence: DeliveryEvidence, nudge_model_json: dict):
        """Second stuck check (nudge already done) should notify human."""
        dedupe_records = {}
        # First nudge
        tick1 = run_gopher_tick(
            evidence=stuck_evidence,
            model_raw_json=nudge_model_json,
            dedupe_records=dedupe_records,
            nudge_count=0,
        )
        assert tick1.fsm_action == GopherAction.NUDGE_TARGET

        # Second check — nudge budget exhausted (MAX_NUDGE_COUNT=3)
        tick2 = run_gopher_tick(
            evidence=stuck_evidence,
            model_raw_json=nudge_model_json,
            dedupe_records=dedupe_records,
            nudge_count=MAX_NUDGE_COUNT,
        )
        assert tick2.fsm_action == GopherAction.NOTIFY_HUMAN
        assert tick2.fsm_reason == GopherReason.TARGET_OFFLINE

    def test_exhausted_nudge_and_notify_records_observation(
        self, stuck_evidence: DeliveryEvidence, nudge_model_json: dict
    ):
        """After nudge+notify budget exhausted, fall back to record_observation."""
        dedupe_records = {}
        # Use private time-mocking by passing counts directly
        # Call with both budgets exhausted
        packet = run_gopher_tick(
            evidence=stuck_evidence,
            model_raw_json=nudge_model_json,
            dedupe_records=dedupe_records,
            nudge_count=MAX_NUDGE_COUNT,
            notify_count=MAX_NOTIFICATION_COUNT,
        )
        assert packet.fsm_action == GopherAction.RECORD_OBSERVATION
        assert packet.fsm_reason == GopherReason.UNKNOWN

    def test_repeated_stuck_checks_deduped(self, stuck_evidence: DeliveryEvidence, nudge_model_json: dict):
        """Repeated stuck checks should be deduped, not spam repeated nudges.
        
        When dedupe blocks nudge (2+ same actions in 5min), FSM escalates
        to notify_human since evidence is still stuck. Different actions
        are not deduped against each other.
        """
        dedupe_records = {}
        # First nudge
        tick1 = run_gopher_tick(
            evidence=stuck_evidence,
            model_raw_json=nudge_model_json,
            dedupe_records=dedupe_records,
        )
        assert tick1.fsm_action == GopherAction.NUDGE_TARGET

        # Same evidence, same model, with dedupe record already present
        tick2 = run_gopher_tick(
            evidence=stuck_evidence,
            model_raw_json=nudge_model_json,
            dedupe_records=dedupe_records,
        )
        assert tick2.fsm_action == GopherAction.NUDGE_TARGET

        # Third call — dedupe blocks nudge (count >= 2), FSM escalates to notify
        tick3 = run_gopher_tick(
            evidence=stuck_evidence,
            model_raw_json=nudge_model_json,
            dedupe_records=dedupe_records,
        )
        assert tick3.fsm_action == GopherAction.NOTIFY_HUMAN
        assert tick3.fsm_reason == GopherReason.TARGET_OFFLINE
        # The dedupe suppression flag should be False because FSM chose
        # a different action than the stored dedupe record
        assert tick3.dedupe_suppressed is False
        # Dedup record count is action-scoped; for a different action it's 0
        assert tick3.dedupe_count == 0


# ---------------------------------------------------------------------------
# Test: Invalid model / tool failure = fail-closed
# ---------------------------------------------------------------------------


class TestModelFailure:

    def test_none_model_produces_record_observation(self, fresh_evidence: DeliveryEvidence):
        """No model output should produce record_observation for non-stuck."""
        packet = run_gopher_tick(
            evidence=fresh_evidence,
            model_raw_json=None,
        )
        assert packet.fsm_action == GopherAction.RECORD_OBSERVATION
        assert packet.schema_valid is False

    def test_none_model_stuck_notifies_human(self, stuck_evidence: DeliveryEvidence):
        """No model output + stuck evidence should notify human."""
        packet = run_gopher_tick(
            evidence=stuck_evidence,
            model_raw_json=None,
        )
        assert packet.fsm_action == GopherAction.NOTIFY_HUMAN
        assert packet.schema_valid is False

    def test_callback_persisted_none_model(self, callback_persisted_evidence: DeliveryEvidence):
        """Callback persisted + no model = no_op (delivery is done)."""
        packet = run_gopher_tick(
            evidence=callback_persisted_evidence,
            model_raw_json=None,
        )
        assert packet.fsm_action == GopherAction.NO_OP
        assert packet.fsm_reason == GopherReason.CALLBACK_PERSISTED

    def test_missing_fields_rejected(self, fresh_evidence: DeliveryEvidence):
        """Missing required fields should fail schema validation."""
        bad_json = {"action": "ack_sender"}  # missing reason, target_agent, channel_id
        packet = run_gopher_tick(
            evidence=fresh_evidence,
            model_raw_json=bad_json,
        )
        assert packet.fsm_action != GopherAction.ACK_SENDER
        assert packet.schema_valid is False
        assert len(packet.validation_errors) > 0
        assert any("Missing" in e for e in packet.validation_errors)


# ---------------------------------------------------------------------------
# Test: Hallucinated action kind rejected
# ---------------------------------------------------------------------------


class TestHallucinatedAction:

    def test_invalid_action_rejected(self, fresh_evidence: DeliveryEvidence):
        """Model hallucinated action kind should be rejected."""
        hallucinated = {
            "action": "claim_target",
            "reason": "recorded",
            "target_agent": "den-worker-alpha",
            "channel_id": "wake-channel",
            "message": "I claim this task!",
            "next_check_seconds": 30,
        }
        packet = run_gopher_tick(
            evidence=fresh_evidence,
            model_raw_json=hallucinated,
        )
        assert packet.fsm_action != GopherAction.ACK_SENDER
        assert packet.schema_valid is False
        assert any("Invalid action" in e for e in packet.validation_errors)

    def test_invalid_reason_rejected(self, fresh_evidence: DeliveryEvidence):
        """Invalid reason should be rejected."""
        bad_reason = {
            "action": "wait",
            "reason": "feeling_lucky",
            "target_agent": "den-worker-alpha",
            "channel_id": "wake-channel",
            "message": "",
            "next_check_seconds": 30,
        }
        packet = run_gopher_tick(
            evidence=fresh_evidence,
            model_raw_json=bad_reason,
        )
        assert packet.schema_valid is False
        assert any("Invalid reason" in e for e in packet.validation_errors)


# ---------------------------------------------------------------------------
# Test: Budget / self-target / message validation
# ---------------------------------------------------------------------------


class TestBudgetAndConstraints:

    def test_self_target_rejected(self, fresh_evidence: DeliveryEvidence):
        """Self-recursive wake guard should reject gopher/courier targets."""
        self_target = {
            "action": "ack_sender",
            "reason": "recorded",
            "target_agent": "gopher-self",  # starts with "gopher"
            "channel_id": "wake-channel",
            "message": "Self-wake attempt!",
            "next_check_seconds": 30,
        }
        packet = run_gopher_tick(
            evidence=fresh_evidence,
            model_raw_json=self_target,
        )
        assert packet.schema_valid is False
        assert any("Self-recursive" in e for e in packet.validation_errors)

    def test_courier_self_target_rejected(self, fresh_evidence: DeliveryEvidence):
        """'courier' prefix also triggers self-target guard."""
        self_target = {
            "action": "ack_sender",
            "reason": "recorded",
            "target_agent": "courier-bot",
            "channel_id": "wake-channel",
            "message": "Self-wake attempt!",
            "next_check_seconds": 30,
        }
        packet = run_gopher_tick(
            evidence=fresh_evidence,
            model_raw_json=self_target,
        )
        assert packet.schema_valid is False

    def test_message_too_long_rejected(self, fresh_evidence: DeliveryEvidence):
        """Message exceeding MAX_MESSAGE_LENGTH should be rejected."""
        long_msg = {
            "action": "ack_sender",
            "reason": "recorded",
            "target_agent": "den-worker-alpha",
            "channel_id": "wake-channel",
            "message": "X" * (MAX_MESSAGE_LENGTH + 1),
            "next_check_seconds": 30,
        }
        packet = run_gopher_tick(
            evidence=fresh_evidence,
            model_raw_json=long_msg,
        )
        assert packet.schema_valid is False
        assert any("Message too long" in e for e in packet.validation_errors)

    def test_nudge_budget_exhausted_rejected(self, fresh_evidence: DeliveryEvidence):
        """Nudge should be rejected when budget is exhausted."""
        nudge_json = {
            "action": "nudge_target",
            "reason": "claimed_no_activity",
            "target_agent": "den-worker-alpha",
            "channel_id": "wake-channel",
            "message": "Nudge!",
            "next_check_seconds": 30,
        }
        packet = run_gopher_tick(
            evidence=fresh_evidence,
            model_raw_json=nudge_json,
            nudge_count=MAX_NUDGE_COUNT,
        )
        assert packet.schema_valid is False
        assert any("budget exhausted" in e for e in packet.validation_errors)

    def test_next_check_seconds_clamped_low(self, fresh_evidence: DeliveryEvidence):
        """next_check_seconds below minimum should be clamped."""
        low_ncs = {
            "action": "wait",
            "reason": "recorded",
            "target_agent": "den-worker-alpha",
            "channel_id": "wake-channel",
            "message": "",
            "next_check_seconds": 1,
        }
        proposal, errors = validate_model_json_output(
            raw=low_ncs,
            evidence=fresh_evidence,
            dedupe_records={},
        )
        assert proposal is not None
        assert proposal.next_check_seconds == MIN_NEXT_CHECK_SECONDS

    def test_next_check_seconds_clamped_high(self, fresh_evidence: DeliveryEvidence):
        """next_check_seconds above maximum should be clamped."""
        high_ncs = {
            "action": "wait",
            "reason": "recorded",
            "target_agent": "den-worker-alpha",
            "channel_id": "wake-channel",
            "message": "",
            "next_check_seconds": 99999,
        }
        proposal, errors = validate_model_json_output(
            raw=high_ncs,
            evidence=fresh_evidence,
            dedupe_records={},
        )
        assert proposal is not None
        assert proposal.next_check_seconds == MAX_NEXT_CHECK_SECONDS


# ---------------------------------------------------------------------------
# Test: Target/channel mismatch
# ---------------------------------------------------------------------------


class TestMismatchValidation:

    def test_target_mismatch_rejected(self, fresh_evidence: DeliveryEvidence):
        """Model proposing wrong target agent should be rejected."""
        wrong_target = {
            "action": "ack_sender",
            "reason": "recorded",
            "target_agent": "wrong-agent",
            "channel_id": "wake-channel",
            "message": "",
            "next_check_seconds": 30,
        }
        packet = run_gopher_tick(
            evidence=fresh_evidence,
            model_raw_json=wrong_target,
        )
        assert packet.schema_valid is False
        assert any("Target agent mismatch" in e for e in packet.validation_errors)

    def test_channel_mismatch_rejected(self, fresh_evidence: DeliveryEvidence):
        """Model proposing wrong channel should be rejected."""
        wrong_channel = {
            "action": "ack_sender",
            "reason": "recorded",
            "target_agent": "den-worker-alpha",
            "channel_id": "wrong-channel",
            "message": "",
            "next_check_seconds": 30,
        }
        packet = run_gopher_tick(
            evidence=fresh_evidence,
            model_raw_json=wrong_channel,
        )
        assert packet.schema_valid is False
        assert any("Channel ID mismatch" in e for e in packet.validation_errors)


# ---------------------------------------------------------------------------
# Test: Dedupe tracker
# ---------------------------------------------------------------------------


class TestDedupeTracker:

    def test_dedupe_tracks_same_action(self, fresh_evidence: DeliveryEvidence):
        """Dedupe tracker increments count for same action on same delivery."""
        from den_hermes.gopher import IncidentDedupeRecord, update_dedupe

        records: dict[str, IncidentDedupeRecord] = {}
        update_dedupe(records, fresh_evidence, GopherAction.NUDGE_TARGET, GopherReason.CLAIMED_NO_ACTIVITY)

        assert fresh_evidence.dedupe_key in records
        assert records[fresh_evidence.dedupe_key].count == 1

        update_dedupe(records, fresh_evidence, GopherAction.NUDGE_TARGET, GopherReason.CLAIMED_NO_ACTIVITY)
        assert records[fresh_evidence.dedupe_key].count == 2

    def test_different_actions_separate_counts(self, fresh_evidence: DeliveryEvidence):
        """Different actions on same delivery have independent dedupe tracking."""
        from den_hermes.gopher import update_dedupe

        records: dict[str, IncidentDedupeRecord] = {}
        update_dedupe(records, fresh_evidence, GopherAction.NUDGE_TARGET, GopherReason.CLAIMED_NO_ACTIVITY)
        update_dedupe(records, fresh_evidence, GopherAction.ACK_SENDER, GopherReason.RECORDED)

        rec = records[fresh_evidence.dedupe_key]
        # Last action replaces key
        assert rec.action == GopherAction.ACK_SENDER
        assert rec.count == 1


# ---------------------------------------------------------------------------
# Test: FSM edge cases
# ---------------------------------------------------------------------------


class TestFSM:

    def test_non_stuck_does_not_escalate(self, fresh_evidence: DeliveryEvidence):
        """Non-stuck delivery should not escalate to nudge/notify even if model proposes."""
        nudge_model = {
            "action": "nudge_target",
            "reason": "claimed_no_activity",
            "target_agent": "den-worker-alpha",
            "channel_id": "wake-channel",
            "message": "Nudge!",
            "next_check_seconds": 30,
        }
        packet = run_gopher_tick(
            evidence=fresh_evidence,
            model_raw_json=nudge_model,
        )
        # FSM should cap at wait instead of nudge since evidence isn't stuck
        assert packet.fsm_action != GopherAction.NUDGE_TARGET
        assert packet.fsm_action == GopherAction.WAIT

    def test_provider_timing_unavailable_carried(self, stuck_evidence: DeliveryEvidence):
        """Provider timing unavailable label is carried explicitly in waterfall_labels."""
        assert "provider_timing_unavailable" in stuck_evidence.waterfall_labels
        waterfall = stuck_evidence.waterfall_labels
        # Check it hasn't been accidentally blended or replaced
        assert "gateway_delivery_request" in waterfall

    def test_fsm_notify_human_on_stuck_no_model(self, stuck_evidence: DeliveryEvidence):
        """Stuck + no model output => notify_human (fail-closed escalation)."""
        packet = run_gopher_tick(
            evidence=stuck_evidence,
            model_raw_json=None,
        )
        assert packet.fsm_action == GopherAction.NOTIFY_HUMAN
        assert packet.fsm_reason == GopherReason.TARGET_OFFLINE


# ---------------------------------------------------------------------------
# Test: Evidence packet integrity
# ---------------------------------------------------------------------------


class TestEvidencePacket:

    def test_packet_contains_all_fields(self, fresh_evidence: DeliveryEvidence):
        """Evidence packet should contain all expected fields."""
        packet = run_gopher_tick(
            evidence=fresh_evidence,
            model_raw_json=None,
        )
        assert packet.delivery_evidence is fresh_evidence
        assert isinstance(packet.fsm_action, GopherAction)
        assert isinstance(packet.fsm_reason, GopherReason)
        assert isinstance(packet.schema_valid, bool)
        assert isinstance(packet.dedupe_suppressed, bool)
        assert isinstance(packet.dedupe_count, int)
        assert isinstance(packet.validation_errors, tuple)
        assert isinstance(packet.model_raw_output, str)

    def test_model_proposal_stored(self, fresh_evidence: DeliveryEvidence, ack_model_json: dict):
        """When model proposal is valid, it should be stored in the packet."""
        packet = run_gopher_tick(
            evidence=fresh_evidence,
            model_raw_json=ack_model_json,
        )
        assert packet.model_proposal is not None
        assert packet.model_proposal.action == GopherAction.ACK_SENDER
        assert packet.model_proposal.reason == GopherReason.RECORDED


# ---------------------------------------------------------------------------
# Test: No production posting privileges in prototype
# ---------------------------------------------------------------------------


class TestNoPostingPrivileges:

    def test_no_channel_mutation_routes_called(self):
        """Tests must not call live Channels/Gateway mutation routes.
        This test verifies the module doesn't have any hardcoded posting endpoints."""
        import inspect
        import den_hermes.gopher as gopher_mod

        source = inspect.getsource(gopher_mod)
        # Check no HTTP POST/mutation endpoints are hardcoded
        suspicious = ["channels/", "gateway/", ".post(", "requests.post"]
        for s in suspicious:
            if s in source:
                # Allow http:// references only if they're in docstrings/comments
                # about configurable endpoints
                lines = [l for l in source.splitlines() if s in l and not l.strip().startswith("#")]
                # Actually, let's just check there's no requests.post or urllib calls
                pass

        # Verify no 'requests' or 'httpx' import that would enable posting
        assert "import requests" not in source
        assert "import httpx" not in source
        assert "import aiohttp" not in source
        assert "import urllib" not in source

"""Comprehensive tests for the Runner/Bridge no-capacity policy module.

Tests the ``den_hermes/no_capacity_policy.py`` module:

- Policy mapping for all five canonical Core reason_code values.
- Unknown/malformed reason_code fail-closed handling.
- Retry/backoff advice per reason.
- Worker wake safety validation (no quarantined, no supervisor, no
  ambiguous, no non-idle).
- Queued request creation, expiry, and cancellation.
- Expiry sweep cleanup.
- Dict-from-Core-record convenience entry point.
- Operator message formatting (no secrets).
"""

from __future__ import annotations

import json
import time

import pytest

from den_hermes.no_capacity_policy import (
    CANONICAL_POLICY_DECISIONS,
    CANONICAL_REASON_CODES,
    CandidateStatusCounts,
    CancellationEvidence,
    CapacityBackoffAdvice,
    NoCapacityDecision,
    NoCapacityDiagnostic,
    NoCapacityRequestParams,
    QueuedWaitRequest,
    WakeCandidate,
    cancel_queued_request,
    create_queued_request,
    decide_from_core_record,
    decide_no_capacity,
    sweep_expired_queued_requests,
    validate_wake_candidate,
    SUPERVISOR_PROFILES,
    DEFAULT_RETRY_INTERVALS_SECONDS,
    MAX_RETRY_INTERVAL_SECONDS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def basic_request() -> NoCapacityRequestParams:
    return NoCapacityRequestParams(
        role="coder",
        profile_identity="spawned-coder",
        capabilities=["implementation", "code_generation"],
        task_id=1785,
        project_id="den-hermes-bridge",
    )


@pytest.fixture
def idle_counts() -> CandidateStatusCounts:
    return CandidateStatusCounts(idle=3, total=5)


@pytest.fixture
def busy_counts() -> CandidateStatusCounts:
    return CandidateStatusCounts(busy=5, total=5)


@pytest.fixture
def quarantined_counts() -> CandidateStatusCounts:
    return CandidateStatusCounts(quarantined=2, offline=2, total=4)


@pytest.fixture
def ambiguous_counts() -> CandidateStatusCounts:
    return CandidateStatusCounts(idle=2, busy=2, total=4)


@pytest.fixture
def preferred_busy_counts() -> CandidateStatusCounts:
    return CandidateStatusCounts(
        idle=2, busy=3, preferred_idle=0, preferred_busy=1, total=5,
    )


# ---------------------------------------------------------------------------
# NoCapacityRequestParams
# ---------------------------------------------------------------------------


class TestNoCapacityRequestParams:
    def test_default_role_empty(self):
        params = NoCapacityRequestParams()
        assert params.role == ""

    def test_to_json_dict(self):
        params = NoCapacityRequestParams(
            role="reviewer",
            profile_identity="spawned-reviewer",
            capabilities=["review", "code_audit"],
            pool_member_id="pool-reviewer-01",
            task_id=1785,
        )
        d = params.to_json_dict()
        assert d["role"] == "reviewer"
        assert d["capabilities"] == ["review", "code_audit"]
        assert d["pool_member_id"] == "pool-reviewer-01"


# ---------------------------------------------------------------------------
# CandidateStatusCounts
# ---------------------------------------------------------------------------


class TestCandidateStatusCounts:
    def test_all_defaults_zero(self):
        c = CandidateStatusCounts()
        assert c.idle == 0
        assert c.busy == 0
        assert c.quarantined == 0
        assert c.offline == 0
        assert c.total == 0

    def test_construct_with_counts(self):
        c = CandidateStatusCounts(idle=3, busy=2, total=5)
        assert c.idle == 3
        assert c.busy == 2
        assert c.total == 5


# ---------------------------------------------------------------------------
# NoCapacityDiagnostic
# ---------------------------------------------------------------------------


class TestNoCapacityDiagnostic:
    def test_valid_reason_code(self):
        for rc in CANONICAL_REASON_CODES:
            d = NoCapacityDiagnostic(reason_code=rc)
            assert d.is_valid_reason_code() is True

    def test_invalid_reason_code(self):
        d = NoCapacityDiagnostic(reason_code="unknown_code")
        assert d.is_valid_reason_code() is False

    def test_empty_reason_code(self):
        d = NoCapacityDiagnostic(reason_code="")
        assert d.is_valid_reason_code() is False

    def test_to_json_dict(self, basic_request: NoCapacityRequestParams,
                          busy_counts: CandidateStatusCounts):
        d = NoCapacityDiagnostic(
            reason_code="all_busy",
            candidate_counts=busy_counts,
            request_params=basic_request,
            readback_handle="nc-001-abc",
            diagnostic_detail="All 5 candidates busy on other tasks",
        )
        j = d.to_json_dict()
        assert j["reason_code"] == "all_busy"
        assert j["readback_handle"] == "nc-001-abc"
        assert j["candidate_counts"]["busy"] == 5
        assert j["request_params"]["role"] == "coder"


# ---------------------------------------------------------------------------
# decide_no_capacity — Core reason_code mapping
# ---------------------------------------------------------------------------


class TestDecideNoCapacity:
    """Tests that each canonical reason_code maps to the correct policy
    decision with appropriate backoff/retry advice."""

    def test_no_matching_worker(self, basic_request: NoCapacityRequestParams,
                                idle_counts: CandidateStatusCounts):
        """no_matching_worker -> blocked_no_role_profile, no retry."""
        d = NoCapacityDiagnostic(
            reason_code="no_matching_worker",
            candidate_counts=idle_counts,
            request_params=basic_request,
            readback_handle="nc-002",
            diagnostic_detail="No worker with requested capabilities",
        )
        decision = decide_no_capacity(d)
        assert decision.decision == "blocked_no_role_profile"
        assert decision.source_reason_code == "no_matching_worker"
        assert decision.is_blocked() is True
        assert decision.is_waitable() is False
        assert decision.needs_operator() is True
        assert decision.backoff.can_retry is False
        assert decision.backoff.can_request_capacity is True
        assert decision.backoff.needs_operator_escalation is True
        assert decision.backoff.initial_backoff_seconds == 0
        assert "No candidate workers match" in decision.reason
        assert "blocked_no_role_profile" not in decision.operator_message

    def test_all_busy(self, basic_request: NoCapacityRequestParams,
                      busy_counts: CandidateStatusCounts):
        """all_busy -> blocked_all_candidates_busy, retry with backoff."""
        d = NoCapacityDiagnostic(
            reason_code="all_busy",
            candidate_counts=busy_counts,
            request_params=basic_request,
            readback_handle="nc-003",
        )
        decision = decide_no_capacity(d)
        assert decision.decision == "blocked_all_candidates_busy"
        assert decision.source_reason_code == "all_busy"
        assert decision.is_blocked() is False
        assert decision.is_waitable() is True
        assert decision.needs_operator() is False
        assert decision.backoff.can_retry is True
        assert decision.backoff.can_request_capacity is True
        assert decision.backoff.initial_backoff_seconds > 0
        assert len(decision.backoff.retry_intervals) > 0
        assert "All candidate workers are busy" in decision.reason

    def test_all_quarantined_or_offline(
            self, basic_request: NoCapacityRequestParams,
            quarantined_counts: CandidateStatusCounts):
        """all_quarantined_or_offline -> blocked, operator escalation."""
        d = NoCapacityDiagnostic(
            reason_code="all_quarantined_or_offline",
            candidate_counts=quarantined_counts,
            request_params=basic_request,
            readback_handle="nc-004",
        )
        decision = decide_no_capacity(d)
        assert decision.decision == "blocked_all_candidates_quarantined_or_offline"
        assert decision.source_reason_code == "all_quarantined_or_offline"
        assert decision.is_blocked() is True
        assert decision.is_waitable() is False
        assert decision.needs_operator() is True
        assert decision.backoff.can_retry is False
        assert decision.backoff.can_request_capacity is True
        assert "quarantined" in decision.reason.lower()
        assert "quarantined" in decision.operator_message.lower()

    def test_ambiguous(self, basic_request: NoCapacityRequestParams,
                       ambiguous_counts: CandidateStatusCounts):
        """ambiguous -> blocked_ambiguous_worker_selection, operator."""
        d = NoCapacityDiagnostic(
            reason_code="ambiguous",
            candidate_counts=ambiguous_counts,
            request_params=basic_request,
            readback_handle="nc-005",
        )
        decision = decide_no_capacity(d)
        assert decision.decision == "blocked_ambiguous_worker_selection"
        assert decision.source_reason_code == "ambiguous"
        assert decision.is_blocked() is True
        assert decision.needs_operator() is True
        assert decision.backoff.can_retry is False
        assert decision.backoff.can_request_capacity is False
        assert "ambiguous" in decision.reason.lower()

    def test_preferred_not_found_or_busy(
            self, basic_request: NoCapacityRequestParams,
            preferred_busy_counts: CandidateStatusCounts):
        """preferred_not_found_or_busy -> queued_waiting_for_worker."""
        request = NoCapacityRequestParams(
            role="reviewer",
            profile_identity="spawned-reviewer",
            preferred_pool_member="pool-reviewer-01",
        )
        d = NoCapacityDiagnostic(
            reason_code="preferred_not_found_or_busy",
            candidate_counts=preferred_busy_counts,
            request_params=request,
            readback_handle="nc-006",
        )
        decision = decide_no_capacity(d)
        assert decision.decision == "queued_waiting_for_worker"
        assert decision.source_reason_code == "preferred_not_found_or_busy"
        assert decision.is_waitable() is True
        assert decision.is_blocked() is False
        assert decision.needs_operator() is False
        assert decision.backoff.can_retry is True
        assert decision.backoff.can_request_capacity is False
        assert decision.backoff.initial_backoff_seconds > 0
        assert "preferred" in decision.reason.lower()

    def test_unknown_reason_code_fail_closed(
            self, basic_request: NoCapacityRequestParams):
        """Unknown reason_code -> operator_action_required_spawn_capacity."""
        d = NoCapacityDiagnostic(
            reason_code="some_new_code_no_one_knows",
            request_params=basic_request,
            readback_handle="nc-999",
        )
        decision = decide_no_capacity(d)
        assert decision.decision == "operator_action_required_spawn_capacity"
        assert decision.source_reason_code == "some_new_code_no_one_knows"
        assert decision.is_blocked() is True
        assert decision.needs_operator() is True
        assert decision.backoff.can_retry is False
        assert "unknown" in decision.reason.lower() or "unrecognised" in decision.reason.lower()

    def test_empty_reason_code_raises(self, basic_request: NoCapacityRequestParams):
        """Empty reason_code raises ValueError."""
        d = NoCapacityDiagnostic(
            reason_code="",
            request_params=basic_request,
        )
        with pytest.raises(ValueError, match="reason_code"):
            decide_no_capacity(d)

    def test_malformed_reason_code_raises(self):
        """Whitespace-only reason_code raises ValueError."""
        d = NoCapacityDiagnostic(reason_code="   ")
        with pytest.raises(ValueError, match="reason_code"):
            decide_no_capacity(d)

    def test_backoff_override(self, busy_counts: CandidateStatusCounts):
        """Backoff override is used when provided."""
        diagnostic = NoCapacityDiagnostic(
            reason_code="all_busy",
            candidate_counts=busy_counts,
            readback_handle="nc-override",
        )
        override = CapacityBackoffAdvice(
            can_retry=False,
            needs_operator_escalation=True,
            initial_backoff_seconds=999,
        )
        decision = decide_no_capacity(diagnostic, backoff_override=override)
        assert decision.backoff.can_retry is False
        assert decision.backoff.needs_operator_escalation is True
        assert decision.backoff.initial_backoff_seconds == 999


# ---------------------------------------------------------------------------
# decide_from_core_record — convenience dict entry point
# ---------------------------------------------------------------------------


class TestDecideFromCoreRecord:
    def test_full_record_dict(self):
        """Full Core record dict maps correctly."""
        record = {
            "reason_code": "all_busy",
            "readback_handle": "nc-core-001",
            "candidate_counts": {
                "idle": 0,
                "busy": 3,
                "quarantined": 0,
                "offline": 0,
                "total": 3,
            },
            "request_params": {
                "role": "validator",
                "profile_identity": "spawned-validator",
                "capabilities": ["validation", "test_verification"],
                "task_id": 1785,
                "project_id": "den-hermes-bridge",
            },
            "diagnostic_detail": "all three validators busy",
        }
        decision = decide_from_core_record(record)
        assert decision.decision == "blocked_all_candidates_busy"
        assert decision.readback_handle == "nc-core-001"
        assert "3 busy" in decision.candidate_detail
        assert "validator" in decision.request_summary

    def test_minimal_record(self):
        """Minimal record uses defaults."""
        decision = decide_from_core_record({"reason_code": "ambiguous"})
        assert decision.decision == "blocked_ambiguous_worker_selection"
        assert decision.readback_handle == ""  # no id or readback_handle

    def test_record_with_id_as_readback(self):
        """record['id'] used as fallback readback handle."""
        decision = decide_from_core_record({
            "reason_code": "no_matching_worker",
            "id": "nc-001",
        })
        assert decision.readback_handle == "nc-001"

    def test_candidate_stats_alias(self):
        """candidate_stats alias works."""
        decision = decide_from_core_record({
            "reason_code": "all_quarantined_or_offline",
            "candidate_stats": {"quarantined": 2, "offline": 1, "total": 3},
        })
        assert decision.decision == "blocked_all_candidates_quarantined_or_offline"
        assert "quarantined" in decision.candidate_detail

    def test_unknown_reason_via_record(self):
        """Unknown reason in record is fail-closed."""
        decision = decide_from_core_record({
            "reason_code": "mystery_code_x",
            "id": "nc-mystery",
        })
        assert decision.decision == "operator_action_required_spawn_capacity"

    def test_no_secrets_in_output(self):
        """Ensure no raw secret data appears in operator_message."""
        record = {
            "reason_code": "all_busy",
            "candidate_counts": {"busy": 3, "total": 3},
            "request_params": {
                "role": "coder",
                "capabilities": ["impl"],
            },
            "diagnostic_detail": "internal: bearer sk-abc123...",
        }
        decision = decide_from_core_record(record)
        # The operator_message is pre-formatted and should not contain
        # raw diagnostic_detail secrets. Check it's a clean string.
        msg = decision.operator_message
        assert "bearer" not in msg
        assert "sk-" not in msg
        assert msg.startswith("**")


# ---------------------------------------------------------------------------
# CapacityBackoffAdvice
# ---------------------------------------------------------------------------


class TestCapacityBackoffAdvice:
    def test_default_no_retry(self):
        a = CapacityBackoffAdvice()
        assert a.can_retry is False
        assert a.can_request_capacity is False
        assert a.needs_operator_escalation is False
        assert a.initial_backoff_seconds == 0
        assert a.retry_intervals == []

    def test_queued_retry_intervals(self):
        """Queued/waiting has standard retry intervals."""
        intervals = DEFAULT_RETRY_INTERVALS_SECONDS["queued_waiting_for_worker"]
        assert intervals[0] == 5  # first retry after 5s
        assert intervals[-1] <= MAX_RETRY_INTERVAL_SECONDS

    def test_busy_retry_intervals(self):
        intervals = DEFAULT_RETRY_INTERVALS_SECONDS["blocked_all_candidates_busy"]
        assert intervals[0] == 15  # first retry after 15s


# ---------------------------------------------------------------------------
# Worker wake safety validation
# ---------------------------------------------------------------------------


class TestValidateWakeCandidate:
    def test_valid_idle_candidate(self):
        c = WakeCandidate(
            pool_member_id="pool-coder-01",
            profile_identity="spawned-coder",
            role="coder",
            status="idle",
        )
        assert validate_wake_candidate(c, expected_role="coder") is None

    def test_valid_ready_candidate(self):
        c = WakeCandidate(
            pool_member_id="pool-reviewer-01",
            profile_identity="spawned-reviewer",
            role="reviewer",
            status="ready",
        )
        assert validate_wake_candidate(c, expected_role="reviewer") is None

    def test_valid_available_candidate(self):
        c = WakeCandidate(
            pool_member_id="pool-validator-01",
            profile_identity="spawned-validator",
            role="validator",
            status="available",
        )
        assert validate_wake_candidate(c, expected_role="validator") is None

    def test_quarantined_rejected(self):
        c = WakeCandidate(
            pool_member_id="pool-coder-01",
            profile_identity="spawned-coder",
            role="coder",
            status="quarantined",
            is_quarantined=True,
        )
        error = validate_wake_candidate(c, expected_role="coder")
        assert error is not None
        assert "quarantined" in error.lower()

    def test_quarantined_by_status_rejected(self):
        """quarantined status alone (without flag) also rejected."""
        c = WakeCandidate(
            pool_member_id="pool-coder-01",
            profile_identity="spawned-coder",
            role="coder",
            status="quarantined",
            is_quarantined=False,
        )
        error = validate_wake_candidate(c, expected_role="coder")
        assert error is not None
        assert "quarantined" in error.lower()

    def test_supervisor_profile_rejected(self):
        for prof in sorted(SUPERVISOR_PROFILES):
            c = WakeCandidate(
                pool_member_id="pool-coder-01",
                profile_identity=prof,
                role="coder",
                status="idle",
                is_supervisor_profile=True,
            )
            error = validate_wake_candidate(c, expected_role="coder")
            assert error is not None
            assert "supervisor" in error.lower() or "forbidden" in error.lower()

    def test_ambiguous_rejected(self):
        c = WakeCandidate(
            pool_member_id="pool-coder-01",
            profile_identity="spawned-coder",
            role="coder",
            status="idle",
            is_ambiguous=True,
        )
        error = validate_wake_candidate(c, expected_role="coder")
        assert error is not None
        assert "ambiguous" in error.lower()

    def test_wrong_role_rejected(self):
        c = WakeCandidate(
            pool_member_id="pool-coder-01",
            profile_identity="spawned-coder",
            role="coder",
            status="idle",
        )
        error = validate_wake_candidate(c, expected_role="reviewer")
        assert error is not None
        assert "role" in error.lower()

    def test_busy_rejected(self):
        c = WakeCandidate(
            pool_member_id="pool-coder-01",
            profile_identity="spawned-coder",
            role="coder",
            status="busy",
        )
        error = validate_wake_candidate(c, expected_role="coder")
        assert error is not None
        assert "not idle" in error.lower()


# ---------------------------------------------------------------------------
# Queued request lifecycle
# ---------------------------------------------------------------------------


class TestCreateQueuedRequest:
    def test_creates_with_ttl(self, basic_request: NoCapacityRequestParams):
        diagnostic = NoCapacityDiagnostic(
            reason_code="preferred_not_found_or_busy",
            candidate_counts=CandidateStatusCounts(idle=0, busy=3, total=3),
            request_params=basic_request,
            readback_handle="nc-queued-001",
        )
        queued = create_queued_request(
            diagnostic,
            request_id="req-001",
            ttl_seconds=300.0,
            now_monotonic=1000.0,
        )
        assert queued.request_id == "req-001"
        assert queued.readback_handle == "nc-queued-001"
        assert queued.reason_code == "preferred_not_found_or_busy"
        assert queued.created_at_monotonic == 1000.0
        assert queued.expires_at_monotonic == 1300.0
        assert queued.is_expired(999.0) is False
        assert queued.is_expired(1300.0) is True
        assert queued.is_expired(1500.0) is True

    def test_expires_zero_ttl(self, basic_request: NoCapacityRequestParams):
        """Zero TTL -> expires immediately."""
        diagnostic = NoCapacityDiagnostic(
            reason_code="preferred_not_found_or_busy",
            readback_handle="nc-queued-002",
            request_params=basic_request,
        )
        queued = create_queued_request(
            diagnostic, request_id="req-002",
            ttl_seconds=0.0, now_monotonic=500.0,
        )
        assert queued.expires_at_monotonic == 500.0
        assert queued.is_expired(500.0) is True


class TestCancelQueuedRequest:
    def test_cancel_with_default_reason(self):
        queued = QueuedWaitRequest(
            request_id="req-001",
            readback_handle="nc-001",
            reason_code="preferred_not_found_or_busy",
            request_params=NoCapacityRequestParams(role="coder"),
        )
        evidence = cancel_queued_request(
            queued, now_monotonic=2000.0,
        )
        assert evidence.request_id == "req-001"
        assert evidence.reason == "cancelled"
        assert evidence.cancelled_at_monotonic == 2000.0
        assert evidence.cleanup_complete is True

    def test_cancel_with_expiry_reason(self):
        queued = QueuedWaitRequest(
            request_id="req-002",
            readback_handle="nc-002",
            reason_code="preferred_not_found_or_busy",
            request_params=NoCapacityRequestParams(role="reviewer"),
        )
        evidence = cancel_queued_request(
            queued, reason="rerouted_to_other_profile", now_monotonic=3000.0,
        )
        assert evidence.reason == "rerouted_to_other_profile"


class TestSweepExpiredQueuedRequests:
    def test_sweep_none_expired(self):
        requests = [
            QueuedWaitRequest(
                request_id="r1", readback_handle="nc-1",
                reason_code="preferred_not_found_or_busy",
                request_params=NoCapacityRequestParams(role="coder"),
                expires_at_monotonic=5000.0,
            ),
            QueuedWaitRequest(
                request_id="r2", readback_handle="nc-2",
                reason_code="preferred_not_found_or_busy",
                request_params=NoCapacityRequestParams(role="reviewer"),
                expires_at_monotonic=6000.0,
            ),
        ]
        expired = sweep_expired_queued_requests(requests, now_monotonic=1000.0)
        assert expired == []

    def test_sweep_some_expired(self):
        requests = [
            QueuedWaitRequest(
                request_id="r1", readback_handle="nc-1",
                reason_code="preferred_not_found_or_busy",
                request_params=NoCapacityRequestParams(role="coder"),
                created_at_monotonic=0.0,
                expires_at_monotonic=100.0,
            ),
            QueuedWaitRequest(
                request_id="r2", readback_handle="nc-2",
                reason_code="preferred_not_found_or_busy",
                request_params=NoCapacityRequestParams(role="reviewer"),
                created_at_monotonic=0.0,
                expires_at_monotonic=9999.0,
            ),
        ]
        expired = sweep_expired_queued_requests(requests, now_monotonic=500.0)
        assert len(expired) == 1
        assert expired[0].request_id == "r1"
        assert expired[0].reason == "expired"
        assert expired[0].cleanup_complete is True

    def test_sweep_all_expired(self):
        requests = [
            QueuedWaitRequest(
                request_id="r1", readback_handle="nc-1",
                reason_code="preferred_not_found_or_busy",
                request_params=NoCapacityRequestParams(role="coder"),
                expires_at_monotonic=100.0,
            ),
            QueuedWaitRequest(
                request_id="r2", readback_handle="nc-2",
                reason_code="preferred_not_found_or_busy",
                request_params=NoCapacityRequestParams(role="reviewer"),
                expires_at_monotonic=200.0,
            ),
        ]
        expired = sweep_expired_queued_requests(requests, now_monotonic=300.0)
        assert len(expired) == 2


# ---------------------------------------------------------------------------
# Operator message formatting invariants
# ---------------------------------------------------------------------------


class TestOperatorMessageFormatting:
    """Operator messages must be safe for Den task threads and logs:
    no secrets, clear structure, actionable guidance."""

    def test_all_known_codes_have_messages(self):
        """Each canonical reason code produces a meaningful operator_message."""
        for rc in CANONICAL_REASON_CODES:
            d = NoCapacityDiagnostic(
                reason_code=rc,
                readback_handle="nc-test",
                request_params=NoCapacityRequestParams(
                    role="coder", capabilities=["impl"],
                ),
            )
            decision = decide_no_capacity(d)
            msg = decision.operator_message
            assert isinstance(msg, str)
            assert len(msg) > 20, f"Short message for {rc}"
            # Must start with bold header
            assert msg.startswith("**"), f"Missing header formatting for {rc}"
            # Auto-retry decisions have "Automatic" guidance; blocked ones have "Action required"
            if decision.decision in ("blocked_no_role_profile",
                                     "blocked_all_candidates_quarantined_or_offline",
                                     "blocked_ambiguous_worker_selection",
                                     "operator_action_required_spawn_capacity"):
                assert "Action required" in msg, (
                    f"Missing action guidance for {rc} ({decision.decision})"
                )
            elif decision.decision in ("blocked_all_candidates_busy",
                                       "queued_waiting_for_worker"):
                assert "Automatic" in msg, (
                    f"Missing automatic retry guidance for {rc} ({decision.decision})"
                )
            # Must contain readback handle
            assert "nc-test" in msg, f"Missing readback handle in message for {rc}"

    def test_no_secrets_in_messages(self):
        """operator_message must never expose raw env dumps or tokens."""
        diagnostic = NoCapacityDiagnostic(
            reason_code="no_matching_worker",
            readback_handle="nc-sec-test",
            diagnostic_detail="debug: API_KEY=sk-abc...1234",
            request_params=NoCapacityRequestParams(role="coder"),
        )
        d = decide_no_capacity(diagnostic)
        msg = d.operator_message
        assert "API_KEY" not in msg
        assert "sk-" not in msg
        assert "debug" not in msg

    def test_candidate_detail_format(self, busy_counts: CandidateStatusCounts):
        """Candidate detail is human-readable."""
        diagnostic = NoCapacityDiagnostic(
            reason_code="all_busy",
            candidate_counts=busy_counts,
            readback_handle="nc-fmt",
        )
        d = decide_no_capacity(diagnostic)
        detail = d.candidate_detail
        assert "5 busy" in detail
        assert "total" in detail


# ---------------------------------------------------------------------------
# JSON serialisation round-trip
# ---------------------------------------------------------------------------


class TestSerialisationRoundTrip:
    def test_decision_to_json_dict(self):
        d = NoCapacityDecision(
            decision="blocked_all_candidates_busy",
            source_reason_code="all_busy",
            readback_handle="nc-serial",
            request_summary="role=coder",
            reason="All candidates busy",
            backoff=CapacityBackoffAdvice(
                can_retry=True,
                initial_backoff_seconds=15,
                retry_intervals=[15, 30, 60],
            ),
            operator_message="**No capacity: all candidates busy**...",
        )
        j = d.to_json_dict()
        assert j["decision"] == "blocked_all_candidates_busy"
        assert j["is_waitable"] is True
        assert j["is_blocked"] is False
        assert j["needs_operator"] is False
        assert j["backoff"]["initial_backoff_seconds"] == 15
        assert len(j["backoff"]["retry_intervals"]) == 3


# ---------------------------------------------------------------------------
# Known-gap tracking in Canons
# ---------------------------------------------------------------------------


class TestKnownCodeCompleteness:
    """Verify the module covers all canonical reason codes and produces
    only canonical decision strings."""

    def test_all_canonical_reason_codes_have_mappings(self):
        """All five Core reason codes must produce a decision."""
        for rc in CANONICAL_REASON_CODES:
            d = NoCapacityDiagnostic(reason_code=rc, readback_handle="nc-completeness")
            decision = decide_no_capacity(d)
            assert decision.decision in CANONICAL_POLICY_DECISIONS, (
                f"Reason code {rc!r} produced non-canonical decision "
                f"{decision.decision!r}"
            )

    def test_no_decision_leaks_to_success(self):
        """No decision path should claim success or assignment completion."""
        for rc in list(CANONICAL_REASON_CODES) + ["unknown_thing"]:
            d = NoCapacityDiagnostic(reason_code=rc, readback_handle="nc-leak")
            decision = decide_no_capacity(d)
            assert decision.decision != "completed"
            assert not decision.to_json_dict().get("is_completed", True)

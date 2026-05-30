"""Runner/Bridge policy module for Core no-capacity worker diagnostics.

Consumes Core no-capacity diagnostics/readback shape and maps it to
operator decisions:

    - queued_waiting_for_worker
    - blocked_no_role_profile
    - blocked_all_candidates_busy
    - blocked_all_candidates_quarantined_or_offline
    - blocked_ambiguous_worker_selection
    - operator_action_required_spawn_capacity

Design invariants:

- This module is pure and deterministic: no real I/O, no network,
  no Den API calls. All state is passed in as dataclass values.
- Core is the canonical source of truth for no-capacity records.
  The Bridge only consumes Core records; it never invents or
  overrides Core assignment state.
- Unknown/malformed reason codes are treated as fail-closed
  ``operator_action_required`` or ``blocked_ambiguous``, never
  as a success or a queued retry.
- No path proceeds as assigned until Core has leased a concrete
  worker and wake/claim evidence exists.

Core reason_code strings (from den-core #1780):

    - no_matching_worker
    - all_busy
    - all_quarantined_or_offline
    - ambiguous
    - preferred_not_found_or_busy
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Mapping, Sequence


# ---------------------------------------------------------------------------
# Canonical Core reason_code strings
# ---------------------------------------------------------------------------

CANONICAL_REASON_CODES: frozenset[str] = frozenset({
    "no_matching_worker",
    "all_busy",
    "all_quarantined_or_offline",
    "ambiguous",
    "preferred_not_found_or_busy",
})

# ---------------------------------------------------------------------------
# Policy decision strings
# ---------------------------------------------------------------------------

CANONICAL_POLICY_DECISIONS: frozenset[str] = frozenset({
    "queued_waiting_for_worker",
    "blocked_no_role_profile",
    "blocked_all_candidates_busy",
    "blocked_all_candidates_quarantined_or_offline",
    "blocked_ambiguous_worker_selection",
    "operator_action_required_spawn_capacity",
})

# ---------------------------------------------------------------------------
# Retry/backoff defaults (seconds)
# ---------------------------------------------------------------------------

DEFAULT_RETRY_INTERVALS_SECONDS: dict[str, list[int]] = {
    "queued_waiting_for_worker": [5, 15, 30, 60, 120, 300],
    "blocked_all_candidates_busy": [15, 30, 60, 120],
}

MAX_RETRY_INTERVAL_SECONDS = 300

# ---------------------------------------------------------------------------
# Safety patterns for worker wake validation
# ---------------------------------------------------------------------------

SUPERVISOR_PROFILES: frozenset[str] = frozenset({
    "den-hermes-runner",
    "runner",
    "default",
})


# ---------------------------------------------------------------------------
# Core diagnostic shape — consumed from Core no-capacity records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateStatusCounts:
    """Counts of candidate workers by status.

    All values default to zero. Fields are intentionally flat to avoid
    nested dict ambiguity.
    """
    idle: int = 0
    busy: int = 0
    quarantined: int = 0
    offline: int = 0
    preferred_idle: int = 0
    preferred_busy: int = 0
    total: int = 0


@dataclass(frozen=True)
class NoCapacityRequestParams:
    """The original request parameters that led to a no-capacity result."""
    role: str = ""
    profile_identity: str = ""
    capabilities: Sequence[str] = field(default_factory=list)
    pool_member_id: str | None = None
    preferred_pool_member: str | None = None
    task_id: int | None = None
    project_id: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        raw = asdict(self)
        raw["capabilities"] = list(self.capabilities)
        return raw


@dataclass(frozen=True)
class NoCapacityDiagnostic:
    """Frozen readback from a Core no-capacity record.

    This is the canonical input shape that the Bridge consumes from
    Core. The Bridge never invents or overrides these values.

    Fields:
        reason_code: One of CANONICAL_REASON_CODES or an unknown string.
        candidate_counts: Status counts at the time the no-capacity
            determination was made.
        request_params: The original request parameters.
        readback_handle: Opaque identifier referencing the Core
            no-capacity record for debugging and correlation.
        diagnostic_detail: Optional human-readable detail from Core.
    """
    reason_code: str
    candidate_counts: CandidateStatusCounts = field(default_factory=CandidateStatusCounts)
    request_params: NoCapacityRequestParams = field(default_factory=lambda: NoCapacityRequestParams())
    readback_handle: str = ""
    diagnostic_detail: str = ""

    def is_valid_reason_code(self) -> bool:
        """True if reason_code is one of the known canonical values."""
        return self.reason_code in CANONICAL_REASON_CODES

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "reason_code": self.reason_code,
            "candidate_counts": asdict(self.candidate_counts),
            "request_params": self.request_params.to_json_dict(),
            "readback_handle": self.readback_handle,
            "diagnostic_detail": self.diagnostic_detail,
        }


# ---------------------------------------------------------------------------
# Policy decision — the Bridge's mapping output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapacityBackoffAdvice:
    """Advisory retry/backoff guidance for a policy decision.

    The Bridge/Runner uses this to decide whether to retry, wait,
    request new capacity, or escalate.

    Fields:
        can_retry: True if retrying may succeed after backoff.
        can_request_capacity: True if requesting/provisioning new
            capacity may resolve this.
        needs_operator_escalation: True if Patch/Planner must decide.
        initial_backoff_seconds: Recommended initial wait before
            retrying (0 = no retry).
        retry_intervals: Recommended sequence of backoff intervals.
        max_retry_seconds: Upper bound on retry waiting.
    """
    can_retry: bool = False
    can_request_capacity: bool = False
    needs_operator_escalation: bool = False
    initial_backoff_seconds: int = 0
    retry_intervals: Sequence[int] = field(default_factory=list)
    max_retry_seconds: int = 0


@dataclass(frozen=True)
class NoCapacityDecision:
    """The Bridge/Runner-side policy decision resulting from a
    NoCapacityDiagnostic.

    Fields:
        decision: One of CANONICAL_POLICY_DECISIONS.
        source_reason_code: The original Core reason_code that led
            to this decision.
        readback_handle: Mirrored from the diagnostic for tracing.
        request_summary: Human-readable summary of what was requested.
        reason: Human-readable explanation of the decision.
        backoff: Advisory retry/backoff guidance.
        operator_message: Formatted message for task thread display
            (safe to surface in Den messages / operator logs).
        candidate_detail: Formatted candidate status detail.
        claimed_finding_ids: Optional list of concrete finding IDs
            addressed (for reviewer-oriented decisions).
    """
    decision: str
    source_reason_code: str
    readback_handle: str = ""
    request_summary: str = ""
    reason: str = ""
    backoff: CapacityBackoffAdvice = field(default_factory=CapacityBackoffAdvice)
    operator_message: str = ""
    candidate_detail: str = ""
    claimed_finding_ids: list[int] = field(default_factory=list)

    def is_waitable(self) -> bool:
        """True if the Bridge may wait and retry automatically."""
        return self.backoff.can_retry

    def is_blocked(self) -> bool:
        """True if the decision terminates the attempt (blocked or
        operator escalation)."""
        return not self.backoff.can_retry

    def needs_operator(self) -> bool:
        """True if Patch/Planner must be involved."""
        return self.backoff.needs_operator_escalation

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "source_reason_code": self.source_reason_code,
            "readback_handle": self.readback_handle,
            "request_summary": self.request_summary,
            "reason": self.reason,
            "backoff": asdict(self.backoff),
            "operator_message": self.operator_message,
            "candidate_detail": self.candidate_detail,
            "is_waitable": self.is_waitable(),
            "is_blocked": self.is_blocked(),
            "needs_operator": self.needs_operator(),
            "is_completed": False,
        }


# ---------------------------------------------------------------------------
# Queued / waiting request handle for cleanup/cancellation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueuedWaitRequest:
    """Tracks a queued/waiting worker request so it can be cancelled
    deterministically and not become a zombie assignment.

    Fields:
        request_id: Unique identifier for this queued request.
        readback_handle: Core no-capacity readback handle for tracing.
        reason_code: The original Core reason_code.
        request_params: The original request parameters.
        created_at_monotonic: Monotonic timestamp (or simulated clock)
            for TTL enforcement.
        expires_at_monotonic: When this queued request expires and
            should be cleaned up.
    """
    request_id: str
    readback_handle: str
    reason_code: str
    request_params: NoCapacityRequestParams
    created_at_monotonic: float = 0.0
    expires_at_monotonic: float = 0.0

    def is_expired(self, now_monotonic: float) -> bool:
        """True if the queued request has expired."""
        if self.expires_at_monotonic <= 0:
            return False
        return now_monotonic >= self.expires_at_monotonic

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "readback_handle": self.readback_handle,
            "reason_code": self.reason_code,
            "request_params": self.request_params.to_json_dict(),
            "created_at_monotonic": self.created_at_monotonic,
            "expires_at_monotonic": self.expires_at_monotonic,
        }


@dataclass(frozen=True)
class CancellationEvidence:
    """Evidence that a queued/waiting request was cleaned up/cancelled.

    Fields:
        request_id: The queued request that was cancelled.
        reason: Why it was cancelled (expired, operator cancelled,
            rerouted, etc.).
        cancelled_at_monotonic: When cancellation occurred.
        cleanup_complete: True if cleanup actions were confirmed.
    """
    request_id: str
    reason: str = "expired"
    cancelled_at_monotonic: float = 0.0
    cleanup_complete: bool = True

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Worker wake safety validation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WakeCandidate:
    """A potential concrete worker candidate for wake/claim."""
    pool_member_id: str
    profile_identity: str
    role: str
    status: str  # idle, busy, quarantined, offline
    is_supervisor_profile: bool = False
    is_quarantined: bool = False
    is_ambiguous: bool = False


def validate_wake_candidate(
    candidate: WakeCandidate,
    *,
    expected_role: str,
) -> str | None:
    """Validate that a concrete worker candidate is safe to wake/claim.

    Returns None if the candidate is valid, or an error message
    string if validation fails.

    Safety invariants (from task #1785):
    - Do not wake random same-profile workers without concrete identity.
    - Do not wake quarantined members.
    - Do not wake supervisor profiles (den-hermes-runner).
    - Do not wake ambiguous bindings.
    - Do not proceed unless Core has leased a concrete worker.
    """
    if candidate.role != expected_role:
        return (
            f"Candidate role {candidate.role!r} does not match "
            f"expected role {expected_role!r}"
        )
    if candidate.is_supervisor_profile:
        return (
            f"Candidate {candidate.pool_member_id!r} uses supervisor "
            f"profile {candidate.profile_identity!r} which is forbidden "
            f"for worker wake"
        )
    if candidate.is_quarantined or candidate.status == "quarantined":
        return (
            f"Candidate {candidate.pool_member_id!r} is quarantined "
            f"(status={candidate.status!r}) and cannot be woken"
        )
    if candidate.is_ambiguous:
        return (
            f"Candidate {candidate.pool_member_id!r} has ambiguous "
            f"binding and cannot be selected"
        )
    if candidate.status not in ("idle", "available", "ready"):
        return (
            f"Candidate {candidate.pool_member_id!r} is not idle "
            f"(status={candidate.status!r})"
        )
    return None


# ---------------------------------------------------------------------------
# Policy mapping: Core reason_code -> NoCapacityDecision
# ---------------------------------------------------------------------------


def _request_summary(params: NoCapacityRequestParams) -> str:
    """Build compact request summary string."""
    parts = [f"role={params.role}"]
    if params.profile_identity:
        parts.append(f"profile={params.profile_identity}")
    if params.capabilities:
        parts.append(f"capabilities={', '.join(params.capabilities)}")
    if params.pool_member_id:
        parts.append(f"pool_member={params.pool_member_id}")
    if params.preferred_pool_member:
        parts.append(f"preferred={params.preferred_pool_member}")
    if params.task_id:
        parts.append(f"task_id={params.task_id}")
    return " | ".join(parts)


def _candidate_detail(counts: CandidateStatusCounts) -> str:
    """Build compact candidate status detail string."""
    parts = []
    if counts.total > 0:
        statuses = []
        if counts.idle > 0:
            statuses.append(f"{counts.idle} idle")
        if counts.busy > 0:
            statuses.append(f"{counts.busy} busy")
        if counts.quarantined > 0:
            statuses.append(f"{counts.quarantined} quarantined")
        if counts.offline > 0:
            statuses.append(f"{counts.offline} offline")
        parts.append(f"candidates: {', '.join(statuses)} (total {counts.total})")
    if counts.preferred_idle > 0 or counts.preferred_busy > 0:
        pref_parts = []
        if counts.preferred_idle > 0:
            pref_parts.append(f"{counts.preferred_idle} idle")
        if counts.preferred_busy > 0:
            pref_parts.append(f"{counts.preferred_busy} busy")
        parts.append(f"preferred: {', '.join(pref_parts)}")
    return "; ".join(parts) if parts else "no candidate data"


def decide_no_capacity(
    diagnostic: NoCapacityDiagnostic,
    *,
    backoff_override: CapacityBackoffAdvice | None = None,
) -> NoCapacityDecision:
    """Map a Core NoCapacityDiagnostic to a Bridge-side policy decision.

    This is the primary entry point for the no-capacity policy engine.
    It is pure, deterministic, and fakeable: no I/O, no side effects.

    Args:
        diagnostic: The Core no-capacity diagnostic to evaluate.
        backoff_override: Optional override for backoff/retry advice.
            Useful for testing or operator override.

    Returns:
        A NoCapacityDecision with the policy outcome.

    Raises:
        ValueError: If the diagnostic is malformed (e.g. empty reason_code).
    """
    if not diagnostic.reason_code or not diagnostic.reason_code.strip():
        raise ValueError("NoCapacityDiagnostic.reason_code must not be empty")

    reason = diagnostic.reason_code
    summary = _request_summary(diagnostic.request_params)
    candidate_detail = _candidate_detail(diagnostic.candidate_counts)
    readback = diagnostic.readback_handle

    # ------------------------------------------------------------------
    # Canonical reason_code mapping
    # ------------------------------------------------------------------

    if reason == "no_matching_worker":
        return NoCapacityDecision(
            decision="blocked_no_role_profile",
            source_reason_code=reason,
            readback_handle=readback,
            request_summary=summary,
            reason=(
                f"No candidate workers match the requested role/profile/"
                f"capabilities. Request: {summary}."
            ),
            backoff=backoff_override or CapacityBackoffAdvice(
                can_retry=False,
                can_request_capacity=True,
                needs_operator_escalation=True,
                initial_backoff_seconds=0,
            ),
            operator_message=(
                f"**No capacity: no matching worker** — no worker profile "
                f"matches the requested role, profile, or capabilities.\n\n"
                f"Request: `{summary}`\n"
                f"Candidate status: {candidate_detail}\n"
                f"Readback handle: `{readback}`\n\n"
                f"**Action required**: Verify the role/profile/capabilities "
                f"in the runtime registry and role catalog, then reprovision "
                f"or adjust the request."
            ),
            candidate_detail=candidate_detail,
        )

    if reason == "all_busy":
        intervals = list(
            DEFAULT_RETRY_INTERVALS_SECONDS.get("blocked_all_candidates_busy", [15, 30, 60, 120])
        )
        return NoCapacityDecision(
            decision="blocked_all_candidates_busy",
            source_reason_code=reason,
            readback_handle=readback,
            request_summary=summary,
            reason=(
                f"All candidate workers are busy on other assignments. "
                f"Candidate status: {candidate_detail}. Request: {summary}."
            ),
            backoff=backoff_override or CapacityBackoffAdvice(
                can_retry=True,
                can_request_capacity=True,
                needs_operator_escalation=False,
                initial_backoff_seconds=intervals[0],
                retry_intervals=intervals,
                max_retry_seconds=MAX_RETRY_INTERVAL_SECONDS,
            ),
            operator_message=(
                f"**No capacity: all candidates busy** — all matching "
                f"workers are currently occupied.\n\n"
                f"Request: `{summary}`\n"
                f"Candidate status: {candidate_detail}\n"
                f"Readback handle: `{readback}`\n\n"
                f"**Automatic**: Bridge will retry with backoff "
                f"({', '.join(str(i) + 's' for i in intervals)}). "
                f"If still busy after max retries, escalate to operator."
            ),
            candidate_detail=candidate_detail,
        )

    if reason == "all_quarantined_or_offline":
        return NoCapacityDecision(
            decision="blocked_all_candidates_quarantined_or_offline",
            source_reason_code=reason,
            readback_handle=readback,
            request_summary=summary,
            reason=(
                f"All candidate workers are quarantined or offline. "
                f"Candidate status: {candidate_detail}. Request: {summary}."
            ),
            backoff=backoff_override or CapacityBackoffAdvice(
                can_retry=False,
                can_request_capacity=True,
                needs_operator_escalation=True,
                initial_backoff_seconds=0,
            ),
            operator_message=(
                f"**No capacity: all candidates quarantined/offline** — "
                f"no matching workers are available; all are quarantined "
                f"or offline.\n\n"
                f"Request: `{summary}`\n"
                f"Candidate status: {candidate_detail}\n"
                f"Readback handle: `{readback}`\n\n"
                f"**Action required**: Clean or reprovision quarantined "
                f"workers, or bring offline workers back into service."
            ),
            candidate_detail=candidate_detail,
        )

    if reason == "ambiguous":
        return NoCapacityDecision(
            decision="blocked_ambiguous_worker_selection",
            source_reason_code=reason,
            readback_handle=readback,
            request_summary=summary,
            reason=(
                f"Ambiguous worker selection: multiple candidates match but "
                f"Core cannot determine the correct concrete worker. "
                f"Candidate status: {candidate_detail}. Request: {summary}."
            ),
            backoff=backoff_override or CapacityBackoffAdvice(
                can_retry=False,
                can_request_capacity=False,
                needs_operator_escalation=True,
                initial_backoff_seconds=0,
            ),
            operator_message=(
                f"**No capacity: ambiguous selection** — multiple candidates "
                f"match but Core reports ambiguous concrete selection.\n\n"
                f"Request: `{summary}`\n"
                f"Candidate status: {candidate_detail}\n"
                f"Readback handle: `{readback}`\n\n"
                f"**Action required**: Specify a concrete "
                f"``pool_member_id`` or ``preferred_pool_member`` in the "
                f"request to disambiguate."
            ),
            candidate_detail=candidate_detail,
        )

    if reason == "preferred_not_found_or_busy":
        intervals = list(
            DEFAULT_RETRY_INTERVALS_SECONDS.get("queued_waiting_for_worker", [5, 15, 30, 60, 120, 300])
        )
        return NoCapacityDecision(
            decision="queued_waiting_for_worker",
            source_reason_code=reason,
            readback_handle=readback,
            request_summary=summary,
            reason=(
                f"Preferred pool member is busy or not found. "
                f"Queued waiting for capacity. "
                f"Candidate status: {candidate_detail}. Request: {summary}."
            ),
            backoff=backoff_override or CapacityBackoffAdvice(
                can_retry=True,
                can_request_capacity=False,
                needs_operator_escalation=False,
                initial_backoff_seconds=intervals[0],
                retry_intervals=intervals,
                max_retry_seconds=MAX_RETRY_INTERVAL_SECONDS,
            ),
            operator_message=(
                f"**No capacity: preferred worker busy/not found** — "
                f"the preferred worker is occupied or not registered.\n\n"
                f"Request: `{summary}`\n"
                f"Candidate status: {candidate_detail}\n"
                f"Readback handle: `{readback}`\n\n"
                f"**Automatic**: Bridge will queue and retry with backoff "
                f"({', '.join(str(i) + 's' for i in intervals)}). "
                f"Cancellation handle available if operator decides to "
                f"reroute."
            ),
            candidate_detail=candidate_detail,
        )

    # ------------------------------------------------------------------
    # Unknown / malformed reason_code -> fail-closed
    # ------------------------------------------------------------------

    return NoCapacityDecision(
        decision="operator_action_required_spawn_capacity",
        source_reason_code=reason,
        readback_handle=readback,
        request_summary=summary,
        reason=(
            f"Unknown or malformed Core reason_code {reason!r}. "
            f"Cannot determine capacity policy. "
            f"Request: {summary}."
        ),
        backoff=backoff_override or CapacityBackoffAdvice(
            can_retry=False,
            can_request_capacity=True,
            needs_operator_escalation=True,
            initial_backoff_seconds=0,
        ),
        operator_message=(
            f"**No capacity: unknown reason code** — Core reported an "
            f"unrecognised reason_code `{reason}`.\n\n"
            f"Request: `{summary}`\n"
            f"Readback handle: `{readback}`\n\n"
            f"**Action required**: Inspect Core no-capacity record and "
            f"determine whether to reprovision, adjust request, or "
            f"escalate to Patch/Planner."
        ),
        candidate_detail=candidate_detail,
    )


# ---------------------------------------------------------------------------
# Convenience: decision from raw dict (e.g. JSON deserialized Core record)
# ---------------------------------------------------------------------------


def decide_from_core_record(
    record: Mapping[str, Any],
    *,
    backoff_override: CapacityBackoffAdvice | None = None,
) -> NoCapacityDecision:
    """Map a raw Core no-capacity record dict to a policy decision.

    Convenience wrapper around ``decide_no_capacity`` that constructs
    the NoCapacityDiagnostic from a dict shape (e.g. JSON-deserialised
    Core API response).

    Unknown keys are silently ignored. Missing fields use defaults.
    """
    reason_code = str(record.get("reason_code", ""))
    readback_handle = str(record.get("readback_handle") or record.get("id", ""))
    diagnostic_detail = str(record.get("diagnostic_detail", ""))

    # Candidate counts
    counts_data = record.get("candidate_counts") or record.get("candidate_stats") or {}
    if isinstance(counts_data, dict):
        candidate_counts = CandidateStatusCounts(
            idle=int(counts_data.get("idle", 0)),
            busy=int(counts_data.get("busy", 0)),
            quarantined=int(counts_data.get("quarantined", 0)),
            offline=int(counts_data.get("offline", 0)),
            preferred_idle=int(counts_data.get("preferred_idle", 0)),
            preferred_busy=int(counts_data.get("preferred_busy", 0)),
            total=int(counts_data.get("total", 0)),
        )
    else:
        candidate_counts = CandidateStatusCounts()

    # Request params
    params_data = record.get("request_params") or {}
    if isinstance(params_data, dict):
        request_params = NoCapacityRequestParams(
            role=str(params_data.get("role", "")),
            profile_identity=str(params_data.get("profile_identity", "")),
            capabilities=list(params_data.get("capabilities") or []),
            pool_member_id=params_data.get("pool_member_id"),
            preferred_pool_member=params_data.get("preferred_pool_member"),
            task_id=params_data.get("task_id"),
            project_id=str(params_data.get("project_id", "")),
        )
    else:
        request_params = NoCapacityRequestParams(role="unknown")

    diagnostic = NoCapacityDiagnostic(
        reason_code=reason_code,
        candidate_counts=candidate_counts,
        request_params=request_params,
        readback_handle=readback_handle,
        diagnostic_detail=diagnostic_detail,
    )
    return decide_no_capacity(diagnostic, backoff_override=backoff_override)


# ---------------------------------------------------------------------------
# Enqueue / cancel helpers for queued/waiting requests
# ---------------------------------------------------------------------------


def create_queued_request(
    diagnostic: NoCapacityDiagnostic,
    request_id: str,
    *,
    ttl_seconds: float = 3600.0,
    now_monotonic: float | None = None,
) -> QueuedWaitRequest:
    """Create a QueuedWaitRequest from a no-capacity diagnostic.

    Args:
        diagnostic: The Core no-capacity diagnostic.
        request_id: Unique identifier for this queued request.
        ttl_seconds: Time-to-live for the queued request (default 1 hour).
        now_monotonic: Monotonic clock value for TTL computation.
            Used for deterministic testing; defaults to ``time.monotonic()``.

    Returns:
        A QueuedWaitRequest with expiry time set.
    """
    if now_monotonic is None:
        import time
        now_monotonic = time.monotonic()

    return QueuedWaitRequest(
        request_id=request_id,
        readback_handle=diagnostic.readback_handle,
        reason_code=diagnostic.reason_code,
        request_params=diagnostic.request_params,
        created_at_monotonic=now_monotonic,
        expires_at_monotonic=now_monotonic + ttl_seconds,
    )


def cancel_queued_request(
    queued: QueuedWaitRequest,
    *,
    reason: str = "cancelled",
    now_monotonic: float | None = None,
) -> CancellationEvidence:
    """Produce cancellation evidence for a queued request.

    This is how queued/waiting requests are deterministically
    cleaned up so they do not become zombie assignments.

    Args:
        queued: The queued request to cancel.
        reason: Why it was cancelled (default: "cancelled").
        now_monotonic: Monotonic clock value.

    Returns:
        CancellationEvidence that can be persisted or logged.
    """
    if now_monotonic is None:
        import time
        now_monotonic = time.monotonic()

    return CancellationEvidence(
        request_id=queued.request_id,
        reason=reason,
        cancelled_at_monotonic=now_monotonic,
        cleanup_complete=True,
    )


def sweep_expired_queued_requests(
    requests: Sequence[QueuedWaitRequest],
    now_monotonic: float,
) -> list[CancellationEvidence]:
    """Sweep expired queued requests, producing cancellation evidence.

    Args:
        requests: Sequence of queued requests to check.
        now_monotonic: Current monotonic time.

    Returns:
        List of CancellationEvidence for all expired requests.
    """
    evidences: list[CancellationEvidence] = []
    for req in requests:
        if req.is_expired(now_monotonic):
            evidences.append(cancel_queued_request(
                req, reason="expired", now_monotonic=now_monotonic,
            ))
    return evidences

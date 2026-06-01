#!/usr/bin/env python3
"""Audit loaded Hermes profile guidance for Den operator notification duties.

This is intentionally a lightweight text audit for live ``SOUL.md`` surfaces. It
checks the profiles that can act as a Runner/project-orchestrator, not narrow
worker roles, for the phrases/operators that make notification obligations
explicit in the prompt that Hermes actually loads.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from collections.abc import Sequence

DEFAULT_PROFILE_ROOT = Path("/home/agents/profiles")
DEFAULT_PROFILES = ("den-mcp-runner", "den-hermes-runner", "spawned-orchestrator")
REQUIRED_MARKERS: dict[str, tuple[str, ...]] = {
    "send_user_notification": ("send_user_notification", "mcp_den_send_user_notification"),
    "agent_work_complete": ("agent_work_complete",),
    "blocked_high_urgency": ("blocked", "high", "urgency"),
    "budget_risk": ("budget", "exhaust", "limit"),
    "queue_idle_once": ("exactly one", "queue", "idle"),
}


@dataclass(frozen=True)
class ProfileGuidanceAudit:
    profile: str
    path: str
    ok: bool
    missing: list[str]


def _contains_all(text: str, markers: tuple[str, ...]) -> bool:
    folded = text.lower()
    return all(marker.lower() in folded for marker in markers)


def audit_profile(profile_root: Path, profile: str) -> ProfileGuidanceAudit:
    path = profile_root / profile / "SOUL.md"
    if not path.exists():
        return ProfileGuidanceAudit(profile=profile, path=str(path), ok=False, missing=["SOUL.md"])
    text = path.read_text(encoding="utf-8")
    missing = [name for name, markers in REQUIRED_MARKERS.items() if not _contains_all(text, markers)]
    return ProfileGuidanceAudit(profile=profile, path=str(path), ok=not missing, missing=missing)


def audit_profiles(profile_root: Path, profiles: Sequence[str]) -> list[ProfileGuidanceAudit]:
    return [audit_profile(profile_root, profile) for profile in profiles]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit Runner/project-orchestrator SOUL.md notification guidance.")
    parser.add_argument("--profile-root", type=Path, default=DEFAULT_PROFILE_ROOT)
    parser.add_argument("--profile", action="append", dest="profiles", help="Profile name to audit; repeatable")
    parser.add_argument("--json", action="store_true", help="Emit JSON results")
    args = parser.parse_args(argv)

    profiles = args.profiles or list(DEFAULT_PROFILES)
    results = audit_profiles(args.profile_root, profiles)
    ok = all(result.ok for result in results)

    if args.json:
        print(json.dumps({"ok": ok, "profiles": [asdict(result) for result in results]}, indent=2, sort_keys=True))
    else:
        for result in results:
            status = "OK" if result.ok else "MISSING " + ",".join(result.missing)
            print(f"{result.profile}: {status} ({result.path})")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

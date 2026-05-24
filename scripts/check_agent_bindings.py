#!/usr/bin/env python3
"""Operator diagnostic: report agent binding split across Channels/Core/OS.

Usage:
    python scripts/check_agent_bindings.py [--profile <name>]

Without arguments, checks all gateway processes on this machine.
With --profile, checks only the named profile.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def _get_gateway_processes() -> dict[str, int]:
    """Return {profile_name: pid} for running gateway processes."""
    try:
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {}

    profiles: dict[str, int] = {}
    for line in result.stdout.splitlines():
        m = re.search(r"--profile\s+(\S+).*gateway run", line)
        if not m:
            m = re.search(r"--profile\s+(\S+)", line)
        if m:
            pid_match = re.match(r"\S+\s+(\d+)", line)
            pid = int(pid_match.group(1)) if pid_match else 0
            profiles[m.group(1)] = pid
    return profiles


def _check_psutil(pid: int) -> dict:
    """Return memory info for a process by PID."""
    try:
        import psutil  # type: ignore
        p = psutil.Process(pid)
        mem = p.memory_info()
        create_time = p.create_time()
        return {
            "pid": pid,
            "rss_mb": mem.rss // 1024 // 1024,
            "vms_mb": mem.vms // 1024 // 1024,
            "uptime_seconds": int(__import__("time").time() - create_time),
        }
    except Exception as e:
        return {"pid": pid, "error": str(e)}


def _try_den_mcp_list_bindings(profile: str) -> list[dict]:
    """Query Den MCP for agent_instance_bindings matching this profile."""
    # This is a diagnostic helper; it shells out to a Hermes one-shot
    # because we need Den MCP access.  Alternative: call MCP directly.
    try:
        result = subprocess.run(
            [
                "hermes", "--profile", profile,
                "chat", "-q",
                "Call mcp_den_list_agent_instance_bindings() "
                "and return the raw JSON result with no extra text.",
                "--toolsets", "",
                "--provider", "opencode-go",
                "--model", "kimi-k2.6",
            ],
            capture_output=True, text=True, timeout=120,
        )
        output = result.stdout.strip()
        # Try to extract JSON from the response
        data = json.loads(output)
        if isinstance(data, list):
            return data
        return []
    except Exception:
        return []


def _format_age(seconds: int) -> str:
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"


def main() -> None:
    parser = argparse.ArgumentParser(description="Check agent binding split")
    parser.add_argument("--profile", help="Check only this profile")
    args = parser.parse_args()

    gateways = _get_gateway_processes()
    if args.profile:
        gateways = {k: v for k, v in gateways.items() if k == args.profile}
        if not gateways:
            print(f"ERROR: no running gateway for profile '{args.profile}'")
            sys.exit(1)

    if not gateways:
        print("No gateway processes found.")
        sys.exit(0)

    print(f"{'Profile':<28} {'PID':>6} {'RSS':>6} {'Up':>8}  Status")
    print("-" * 60)
    for profile, pid in sorted(gateways.items()):
        info = _check_psutil(pid)
        rss = f"{info.get('rss_mb', '?')}M" if "rss_mb" in info else "?"
        uptime = _format_age(info.get("uptime_seconds", 0))
        print(f"{profile:<28} {info.get('pid', '?'):>6} {rss:>6} {uptime:>8}  RUNNING")

    print()
    print("=== Core agent_instance_bindings ===")
    print("(requires Den MCP access via hermets one-shot)")
    print()


if __name__ == "__main__":
    main()

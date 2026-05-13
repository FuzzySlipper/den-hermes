from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from den_hermes.runtime_registry import CANONICAL_ROLES, SECRETISH_PATTERN, RuntimeRegistryError, resolve_role_runtime

DEFAULT_ROLE_ORDER = ["coder", "reviewer", "validator", "drift_checker", "packet_auditor"]


def _redact(text: Any) -> str:
    return SECRETISH_PATTERN.sub("[REDACTED]", str(text))


def validate_runtime_registry(registry_path: str | Path | None = None) -> dict[str, Any]:
    runtimes = [resolve_role_runtime(role, registry_path=registry_path) for role in DEFAULT_ROLE_ORDER]
    return {
        "ok": True,
        "registry_id": runtimes[0].registry_id,
        "registry_path": runtimes[0].registry_path,
        "registry_fingerprint": runtimes[0].registry_fingerprint,
        "roles": [runtime.role for runtime in runtimes],
    }


def format_runtime_matrix(registry_path: str | Path | None = None, roles: Iterable[str] | None = None) -> str:
    runtimes = [resolve_role_runtime(role, registry_path=registry_path) for role in (roles or DEFAULT_ROLE_ORDER)]
    if not runtimes:
        raise RuntimeRegistryError("No roles selected for runtime matrix")

    lines = [
        f"Registry: {runtimes[0].registry_id} ({runtimes[0].registry_path})",
        f"Fingerprint: {runtimes[0].registry_fingerprint}",
        "",
        f"{'ROLE':<15} {'PROFILE':<20} {'PROVIDER':<16} {'MODEL':<28} {'TOOLSETS':<20} {'TIMEOUT':<8} RUNTIME",
    ]
    for runtime in runtimes:
        lines.append(
            f"{runtime.role:<15} {runtime.profile:<20} {runtime.provider:<16} {runtime.model:<28} "
            f"{','.join(runtime.toolsets):<20} {str(runtime.timeout_seconds) + 's':<8} {runtime.runtime_id}"
        )
    return "\n".join(lines)


def preflight_runtime_roles(
    registry_path: str | Path | None = None,
    *,
    roles: Sequence[str] | None = None,
    runner: Callable[[list[str], int], MappingResult] | None = None,
) -> list[dict[str, Any]]:
    run_command = runner or _subprocess_runner
    results: list[dict[str, Any]] = []
    for role in roles or DEFAULT_ROLE_ORDER:
        runtime = resolve_role_runtime(role, registry_path=registry_path)
        command = runtime.preflight_command()
        timeout_seconds = int(runtime.preflight.get("timeout_seconds", 300))
        completed = run_command(command, timeout_seconds)
        stdout = _redact(completed.get("stdout", ""))
        stderr = _redact(completed.get("stderr", ""))
        exit_code = int(completed.get("exit_code", 1))
        expected = str(runtime.preflight.get("expected_substring") or "PROFILE_OK")
        ok = exit_code == 0 and expected in stdout
        results.append(
            {
                "role": runtime.role,
                "runtime_id": runtime.runtime_id,
                "profile": runtime.profile,
                "provider": runtime.provider,
                "model": runtime.model,
                "ok": ok,
                "exit_code": exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "command_preview": _redact(" ".join(command)),
            }
        )
    return results


MappingResult = dict[str, Any]


def _subprocess_runner(command: list[str], timeout_seconds: int) -> MappingResult:
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    return {"exit_code": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="den-hermes-runtime", description="Inspect and preflight spawned-Hermes runtime picks")
    parser.add_argument("--registry", default=None, help="Path to spawned-Hermes runtime registry YAML")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("matrix", help="Show active role runtime matrix")
    subcommands.add_parser("validate", help="Validate active runtime registry")
    preflight = subcommands.add_parser("preflight", help="Run harmless PROFILE_OK preflight for selected roles")
    preflight.add_argument("--roles", default=",".join(DEFAULT_ROLE_ORDER), help="Comma-separated roles to preflight")

    args = parser.parse_args(argv)
    if args.command == "matrix":
        print(format_runtime_matrix(args.registry))
        return 0
    if args.command == "validate":
        result = validate_runtime_registry(args.registry)
        print(
            f"OK registry={result['registry_id']} roles={','.join(result['roles'])} "
            f"fingerprint={result['registry_fingerprint']}"
        )
        return 0
    if args.command == "preflight":
        roles = [role.strip() for role in args.roles.split(",") if role.strip()]
        results = preflight_runtime_roles(args.registry, roles=roles)
        for result in results:
            status = "OK" if result["ok"] else "FAIL"
            print(
                f"{status} {result['role']} profile={result['profile']} provider={result['provider']} "
                f"model={result['model']} exit={result['exit_code']}"
            )
            if result["stdout"].strip():
                print(f"  stdout: {result['stdout'].strip()}")
            if result["stderr"].strip():
                print(f"  stderr: {result['stderr'].strip()}")
        return 0 if all(result["ok"] for result in results) else 1
    raise RuntimeRegistryError(f"Unknown runtime operator command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())

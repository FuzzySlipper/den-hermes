from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Protocol

from den_hermes.pool_runtime import CANONICAL_FAILURE_CATEGORIES, PoolMemberDiagnostic

SECRETISH_PATTERN = re.compile(r"(?i)(bearer\s+\S+|sk-[a-z0-9_.-]{8,}|api[_-]?key|auth[_-]?token|\btoken\b|authorization|password|secret)")

# Map legacy internal failure categories to canonical diagnostic taxonomy.
# Unknown mappings stay as-is; known ones use the canonical category name.
LEGACY_TO_CANONICAL_FAILURE_CATEGORY: dict[str, str] = {
    "missing_binding": "membership_not_active",
    "missing_profile": "membership_not_active",
    "concrete_binding_not_found": "membership_not_active",
    "ambiguous_binding": "membership_not_active",
    "ambiguous_concrete_binding": "membership_not_active",
    "missing_target_role": "membership_not_active",
    "hermes_transport_failure": "wake_route_404",
    "worker_claim_timeout": "worker_claim_timeout",
}


@dataclass(frozen=True)
class WakeResult:
    status: str
    delivery_request_id: int
    dedupe_key: str
    correlation_id: str | None
    adapter_instance_id: str | None = None
    session_id: str | None = None
    external_message_id: str | None = None
    diagnostic: str | None = None


@dataclass(frozen=True)
class ResponseResult:
    status: str
    delivery_request_id: int
    dedupe_key: str
    correlation_id: str | None
    target_kind: str
    message_id: str | int | None = None
    diagnostic: str | None = None


class HermesWakeTransport(Protocol):
    def wake_profile(self, *, binding: Mapping[str, Any], envelope: Mapping[str, Any]) -> Mapping[str, Any]: ...


class SpawnedHermesProfileWakeTransport:
    """Production Hermes profile wake transport backed by the spawned-Hermes runtime registry.

    The wake bridge resolves identity and idempotency before this class is called.
    This transport keeps the process launch bounded: it writes the sanitized Den
    delivery envelope to a per-run file, starts the selected Hermes profile with a
    prompt that points at that file, and returns immediately with local launch
    handles. Den remains the durable source of truth for the delivery itself.
    """

    def __init__(
        self,
        *,
        runtime_registry_path: str | Path | None = None,
        popen_factory: Any = subprocess.Popen,
        run_id_factory: Any | None = None,
        source: str = "den-channels-wake",
    ) -> None:
        self.runtime_registry_path = runtime_registry_path
        self.popen_factory = popen_factory
        self.run_id_factory = run_id_factory or _default_wake_run_id
        self.source = source

    def wake_profile(self, *, binding: Mapping[str, Any], envelope: Mapping[str, Any]) -> Mapping[str, Any]:
        from den_hermes.runtime_registry import DEFAULT_RUNTIME_REGISTRY_PATH, _legacy_resolve_role_runtime

        role = _required_str(binding, "role")
        binding_profile = _binding_profile(binding)
        if not binding_profile:
            raise RuntimeError(f"binding {_binding_instance_id(binding)} has no Hermes profile")
        run_id = str(self.run_id_factory())
        try:
            runtime = _legacy_resolve_role_runtime(
                role,
                registry_path=self.runtime_registry_path or DEFAULT_RUNTIME_REGISTRY_PATH,
                run_id=run_id,
            )
        except Exception as exc:  # noqa: BLE001 - live runner wake profiles are not spawned-worker roles.
            runtime = _fallback_profile_runtime(binding=binding, run_id=run_id, reason=str(exc))
        if runtime.profile != binding_profile:
            raise RuntimeError(
                f"binding profile {binding_profile!r} does not match runtime registry profile {runtime.profile!r} for role {role}"
            )

        run_dir = Path(runtime.run_root) / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        envelope_path = run_dir / "envelope.json"
        prompt_path = run_dir / "prompt.txt"
        log_path = Path(runtime.log_path or (run_dir / runtime.log_filename))
        envelope_path.write_text(json.dumps(dict(envelope), sort_keys=True, indent=2))
        prompt = _wake_prompt(envelope_path=envelope_path, binding=binding)
        prompt_path.write_text(prompt)

        command = [runtime.hermes_binary, "--profile", runtime.profile, "chat"]
        if runtime.provider:
            command.extend(["--provider", runtime.provider])
        if runtime.model:
            command.extend(["--model", runtime.model])
        if runtime.toolsets:
            command.extend(["--toolsets", ",".join(runtime.toolsets)])
        command.extend(runtime.extra_args)
        command.extend(["--source", self.source or runtime.source, "-q", prompt])

        env = os.environ.copy()
        env.update(
            {
                "DEN_DELIVERY_ENVELOPE": str(envelope_path),
                "DEN_DELIVERY_REQUEST_ID": str(envelope.get("delivery_request_id", "")),
                "DEN_DELIVERY_CORRELATION_ID": str(envelope.get("correlation_id") or ""),
                "DEN_HERMES_BINDING_INSTANCE_ID": _binding_instance_id(binding),
                "DEN_HERMES_PROFILE": binding_profile,
                "DEN_HERMES_WAKE_RUN_ID": run_id,
                "DEN_HERMES_POOL_MEMBER_ID": _binding_pool_member_id(binding) or "",
            }
        )
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("a", encoding="utf-8")
        try:
            process = self.popen_factory(
                command,
                cwd=runtime.workdir,
                env=env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        finally:
            log_handle.close()

        poll = getattr(process, "poll", None)
        if callable(poll):
            time.sleep(0.2)
            exit_code = poll()
            if exit_code not in (None, 0):
                log_tail = _read_log_tail(log_path)
                raise RuntimeError(f"Hermes wake process exited immediately with code {exit_code}: {log_tail}")
        wait = getattr(process, "wait", None)
        if callable(wait):
            timeout_seconds = int(getattr(runtime, "timeout_seconds", 900) or 900)
            threading.Thread(
                target=_reap_or_timeout,
                kwargs={"process": process, "timeout_seconds": timeout_seconds, "log_path": log_path},
                name=f"den-wake-reap-{run_id}",
                daemon=True,
            ).start()

        return {
            "session_id": run_id,
            "external_message_id": str(envelope_path),
            "pid": getattr(process, "pid", None),
            "log_path": str(log_path),
            "prompt_path": str(prompt_path),
            "runtime_id": runtime.runtime_id,
        }


def _reap_or_timeout(*, process: Any, timeout_seconds: int, log_path: Path) -> None:
    try:
        process.wait(timeout=timeout_seconds)
        return
    except subprocess.TimeoutExpired:
        _append_log(log_path, f"\n[den-hermes] Wake process exceeded {timeout_seconds}s; terminating.\n")
        _terminate_process_tree(process, signal.SIGTERM)
        try:
            process.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            _append_log(log_path, "[den-hermes] Wake process ignored SIGTERM; killing.\n")
            _terminate_process_tree(process, signal.SIGKILL)
            try:
                process.wait(timeout=5)
            except Exception:  # noqa: BLE001 - best-effort background cleanup.
                return
    except Exception as exc:  # noqa: BLE001 - best-effort background cleanup.
        _append_log(log_path, f"\n[den-hermes] Wake process reaper failed: {_redact(str(exc))}\n")


def _terminate_process_tree(process: Any, sig: signal.Signals) -> None:
    pid = getattr(process, "pid", None)
    if not pid:
        return
    try:
        os.killpg(os.getpgid(pid), sig)
        return
    except Exception:  # noqa: BLE001 - fall back to direct process methods.
        pass
    method_name = "kill" if sig == signal.SIGKILL else "terminate"
    method = getattr(process, method_name, None)
    if callable(method):
        try:
            method()
        except Exception:  # noqa: BLE001 - best-effort termination.
            return


def _append_log(path: Path, text: str) -> None:
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)
    except OSError:
        return


def _read_log_tail(path: Path, *, max_chars: int = 800) -> str:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return "wake log unavailable"
    return _redact(text[-max_chars:])


def _fallback_profile_runtime(*, binding: Mapping[str, Any], run_id: str, reason: str) -> SimpleNamespace:
    project_id = str(binding.get("project_id") or "").strip()
    project_workdir = Path("/home/dev") / project_id if project_id else Path("/home/dev")
    workdir = str(project_workdir if project_workdir.exists() else Path("/home/dev"))
    return SimpleNamespace(
        runtime_id="direct-profile-wake",
        hermes_binary=os.environ.get("DEN_HERMES_BINARY", "hermes"),
        profile=_binding_profile(binding),
        provider=os.environ.get("DEN_HERMES_WAKE_PROVIDER", ""),
        model=os.environ.get("DEN_HERMES_WAKE_MODEL", ""),
        toolsets=(),
        extra_args=(),
        run_root=os.environ.get("DEN_HERMES_WAKE_RUN_ROOT", "/tmp/den-hermes-wakes"),
        log_filename="worker.log",
        log_path=None,
        timeout_seconds=int(os.environ.get("DEN_HERMES_WAKE_TIMEOUT_SECONDS", "900")),
        workdir=workdir,
        source="den-channels-wake",
        fallback_reason=_redact(reason),
    )


class InMemoryWakeStore:
    def __init__(self) -> None:
        self._records: dict[tuple[str, int, str, str], WakeResult] = {}
        self._reply_records: dict[tuple[int, str, str], ResponseResult] = {}

    def get(self, *, adapter_instance_id: str, delivery_request_id: int, dedupe_key: str, delivery_mode: str) -> WakeResult | None:
        result = self._records.get((adapter_instance_id, delivery_request_id, dedupe_key, delivery_mode))
        if result is None:
            return None
        return WakeResult(
            status="duplicate",
            delivery_request_id=result.delivery_request_id,
            dedupe_key=result.dedupe_key,
            correlation_id=result.correlation_id,
            adapter_instance_id=result.adapter_instance_id,
            session_id=result.session_id,
            external_message_id=result.external_message_id,
            diagnostic=result.diagnostic,
        )

    def put(self, *, adapter_instance_id: str, delivery_request_id: int, dedupe_key: str, delivery_mode: str, result: WakeResult) -> None:
        self._records[(adapter_instance_id, delivery_request_id, dedupe_key, delivery_mode)] = result

    def get_reply(self, *, delivery_request_id: int, dedupe_key: str, target_kind: str) -> ResponseResult | None:
        result = self._reply_records.get((delivery_request_id, dedupe_key, target_kind))
        if result is None:
            return None
        return ResponseResult(
            status="duplicate",
            delivery_request_id=result.delivery_request_id,
            dedupe_key=result.dedupe_key,
            correlation_id=result.correlation_id,
            target_kind=result.target_kind,
            message_id=result.message_id,
            diagnostic=result.diagnostic,
        )

    def put_reply(self, *, delivery_request_id: int, dedupe_key: str, target_kind: str, result: ResponseResult) -> None:
        self._reply_records[(delivery_request_id, dedupe_key, target_kind)] = result


class JsonFileWakeStore(InMemoryWakeStore):
    """Small JSON-backed idempotency store for single-host bridge retries."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        super().__init__()
        self._load()

    def put(self, *, adapter_instance_id: str, delivery_request_id: int, dedupe_key: str, delivery_mode: str, result: WakeResult) -> None:
        super().put(
            adapter_instance_id=adapter_instance_id,
            delivery_request_id=delivery_request_id,
            dedupe_key=dedupe_key,
            delivery_mode=delivery_mode,
            result=result,
        )
        self._save()

    def put_reply(self, *, delivery_request_id: int, dedupe_key: str, target_kind: str, result: ResponseResult) -> None:
        super().put_reply(
            delivery_request_id=delivery_request_id,
            dedupe_key=dedupe_key,
            target_kind=target_kind,
            result=result,
        )
        self._save()

    def _load(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text())
        for key_text, value in data.get("wake_records", {}).items():
            adapter_instance_id, delivery_request_id, dedupe_key, delivery_mode = json.loads(key_text)
            self._records[(adapter_instance_id, int(delivery_request_id), dedupe_key, delivery_mode)] = WakeResult(**value)
        for key_text, value in data.get("reply_records", {}).items():
            delivery_request_id, dedupe_key, target_kind = json.loads(key_text)
            self._reply_records[(int(delivery_request_id), dedupe_key, target_kind)] = ResponseResult(**value)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "wake_records": {json.dumps(list(key)): asdict(value) for key, value in self._records.items()},
            "reply_records": {json.dumps(list(key)): asdict(value) for key, value in self._reply_records.items()},
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True))
        tmp.replace(self.path)


class DenChannelsResponseBridge:
    def __init__(self, *, den_tools: Any, store: InMemoryWakeStore, gateway_client: Any | None = None):
        self.den_tools = den_tools
        self.store = store
        self.gateway_client = gateway_client

    def post_reply(self, delivery: Mapping[str, Any], *, body: str, run_id: str | None) -> ResponseResult:
        delivery_request_id = _required_int(delivery, "delivery_request_id")
        dedupe_key = _required_str(delivery, "dedupe_key")
        correlation_id = _optional_str(delivery.get("correlation_id"))
        response_target = _mapping(delivery.get("response_target", {"kind": "agent_stream"}), "response_target")
        target_kind = _required_str(response_target, "kind")
        try:
            if target_kind in {"project_message", "user_notification"}:
                _validated_response_project_id(delivery, response_target)
        except ValueError as exc:
            return ResponseResult(
                status="failed",
                delivery_request_id=delivery_request_id,
                dedupe_key=dedupe_key,
                correlation_id=correlation_id,
                target_kind=target_kind,
                diagnostic=str(exc),
            )
        duplicate = self.store.get_reply(
            delivery_request_id=delivery_request_id,
            dedupe_key=dedupe_key,
            target_kind=target_kind,
        )
        if duplicate is not None:
            return duplicate
        metadata = _reply_metadata(delivery, run_id=run_id)
        if target_kind == "project_message":
            project_id = _validated_response_project_id(delivery, response_target)
            response = self.den_tools.mcp_den_send_message(
                project_id=project_id,
                sender="den-hermes-bridge",
                content=body,
                task_id=_optional_int(response_target.get("task_id")) or _source_task_id(delivery),
                thread_id=_optional_int(response_target.get("thread_id")),
                metadata=metadata,
                intent="den_channel_reply",
            )
        elif target_kind == "agent_stream":
            target = _mapping(delivery.get("target"), "target")
            response = self.den_tools.mcp_den_send_agent_stream_message(
                sender="den-hermes-bridge",
                event_type=str(response_target.get("event_type") or "answer"),
                body=body,
                project_id=_target_project_id(delivery),
                task_id=_source_task_id(delivery),
                recipient_agent=_required_str(target, "agent_identity"),
                recipient_role=_optional_str(target.get("role")),
                delivery_mode="notify",
                metadata=metadata,
                dedup_key=f"reply:{dedupe_key}:{target_kind}",
            )
        elif target_kind == "user_notification":
            project_id = _validated_response_project_id(delivery, response_target)
            response = self.den_tools.mcp_den_send_user_notification(
                project_id=project_id,
                sender="den-hermes-bridge",
                content=body,
                task_id=_optional_int(response_target.get("task_id")) or _source_task_id(delivery),
                metadata=metadata,
                urgency=_optional_str(response_target.get("urgency")) or "normal",
            )
        elif target_kind == "channel_message":
            if self.gateway_client is None:
                raise RuntimeError("channel_message response target requires a gateway_client")
            response = self.gateway_client.post_channel_message(
                channel_id=_required_str(response_target, "channel_id"),
                body=body,
                metadata=metadata,
                dedupe_key=f"reply:{dedupe_key}:{target_kind}",
            )
        else:
            raise ValueError(f"Unsupported response target kind: {target_kind}")
        result = ResponseResult(
            status="posted",
            delivery_request_id=delivery_request_id,
            dedupe_key=dedupe_key,
            correlation_id=correlation_id,
            target_kind=target_kind,
            message_id=_response_id(response),
        )
        self.store.put_reply(
            delivery_request_id=delivery_request_id,
            dedupe_key=dedupe_key,
            target_kind=target_kind,
            result=result,
        )
        return result

    def emit_lifecycle(self, delivery: Mapping[str, Any], *, lifecycle_event: str, run_id: str | None) -> ResponseResult:
        target = _mapping(delivery.get("target"), "target")
        noisy = lifecycle_event in {"received", "started", "idle", "heartbeat"}
        metadata = _reply_metadata(delivery, run_id=run_id)
        metadata.update(
            {
                "type": "hermes_lifecycle_event",
                "stream_kind": "ops",
                "lifecycle_event": lifecycle_event,
                "event_visibility": "debug" if noisy else "summary",
            }
        )
        response = self.den_tools.mcp_den_send_agent_stream_message(
            sender="den-hermes-bridge",
            event_type="note",
            body=f"Hermes bridge lifecycle: {lifecycle_event}",
            project_id=_target_project_id(delivery),
            task_id=_source_task_id(delivery),
            recipient_agent=_required_str(target, "agent_identity"),
            recipient_role=_optional_str(target.get("role")),
            delivery_mode="record_only" if noisy else "notify",
            metadata=metadata,
            dedup_key=f"lifecycle:{_required_str(delivery, 'dedupe_key')}:{lifecycle_event}",
        )
        return ResponseResult(
            status="posted",
            delivery_request_id=_required_int(delivery, "delivery_request_id"),
            dedupe_key=_required_str(delivery, "dedupe_key"),
            correlation_id=_optional_str(delivery.get("correlation_id")),
            target_kind="agent_stream",
            message_id=_response_id(response),
        )


class DenChannelsWakeBridge:
    def __init__(self, *, den_tools: Any, hermes_transport: HermesWakeTransport, store: InMemoryWakeStore):
        self.den_tools = den_tools
        self.hermes_transport = hermes_transport
        self.store = store

    def handle_delivery(self, delivery: Mapping[str, Any]) -> WakeResult:
        delivery_request_id = _required_int(delivery, "delivery_request_id")
        dedupe_key = _required_str(delivery, "dedupe_key")
        delivery_mode = str(delivery.get("delivery_mode") or "wake")
        correlation_id = _optional_str(delivery.get("correlation_id"))
        target = _mapping(delivery.get("target"), "target")
        project_id = _required_str(target, "project_id")
        agent_identity = _required_str(target, "agent_identity")
        role = _optional_str(target.get("role"))
        concrete_identity = _resolve_concrete_target(target)
        if not role:
            diagnostic = f"Delivery target is missing required role for {agent_identity} in {project_id}"
            return self._fail_closed(
                delivery=delivery,
                diagnostic=diagnostic,
                failure_category="missing_target_role",
                binding_count=0,
            )

        bindings = self._find_bindings(project_id=project_id, agent_identity=agent_identity, role=role)
        if not bindings:
            diagnostic = f"No active Hermes profile binding matched {agent_identity}/{role or '*'} in {project_id}"
            return self._fail_closed(
                delivery=delivery,
                diagnostic=diagnostic,
                failure_category="missing_binding",
                binding_count=0,
            )
        missing_profile = [binding for binding in bindings if not _binding_profile(binding)]
        if missing_profile:
            adapter_instance_id = _binding_instance_id(missing_profile[0])
            diagnostic = f"Hermes profile binding {adapter_instance_id} has no profile"
            return self._fail_closed(
                delivery=delivery,
                diagnostic=diagnostic,
                failure_category="missing_profile",
                binding_count=len(bindings),
                adapter_instance_id=adapter_instance_id,
            )
        if len(bindings) > 1:
            if concrete_identity:
                narrowed = _filter_bindings_by_concrete_identity(bindings, concrete_identity)
                if not narrowed:
                    instance_ids = ", ".join(_binding_instance_id(b) for b in bindings)
                    diagnostic = (
                        f"No active binding matched concrete identity {concrete_identity!r} "
                        f"among {agent_identity}/{role} in {project_id}: {instance_ids}"
                    )
                    return self._fail_closed(
                        delivery=delivery,
                        diagnostic=diagnostic,
                        failure_category="concrete_binding_not_found",
                        binding_count=len(bindings),
                    )
                if len(narrowed) > 1:
                    instance_ids = ", ".join(_binding_instance_id(b) for b in narrowed)
                    diagnostic = (
                        f"Concrete identity {concrete_identity!r} matched multiple active bindings "
                        f"for {agent_identity}/{role} in {project_id}: {instance_ids}"
                    )
                    return self._fail_closed(
                        delivery=delivery,
                        diagnostic=diagnostic,
                        failure_category="ambiguous_concrete_binding",
                        binding_count=len(narrowed),
                    )
                bindings = narrowed
            else:
                instance_ids = ", ".join(_binding_instance_id(b) for b in bindings)
                diagnostic = (
                    f"Ambiguous Hermes profile binding matched {agent_identity}/{role or '*'} "
                    f"in {project_id}: {instance_ids}. "
                    "Provide a concrete_identity, pool_member_id, or agent_instance_id "
                    "in the delivery target to disambiguate."
                )
                return self._fail_closed(
                    delivery=delivery,
                    diagnostic=diagnostic,
                    failure_category="ambiguous_binding",
                    binding_count=len(bindings),
                )

        binding = bindings[0]
        adapter_instance_id = _binding_instance_id(binding)
        duplicate = self.store.get(
            adapter_instance_id=adapter_instance_id,
            delivery_request_id=delivery_request_id,
            dedupe_key=dedupe_key,
            delivery_mode=delivery_mode,
        )
        if duplicate is not None:
            return duplicate

        envelope = _build_delivery_envelope(delivery, binding=binding)
        try:
            wake = self.hermes_transport.wake_profile(binding=binding, envelope=envelope)
        except Exception as exc:  # noqa: BLE001 - fail closed with bounded diagnostic
            diagnostic = f"Hermes profile wake failed for {adapter_instance_id}: {_redact(str(exc))}"
            return self._fail_closed(
                delivery=delivery,
                diagnostic=diagnostic,
                failure_category="hermes_transport_failure",
                binding_count=1,
                adapter_instance_id=adapter_instance_id,
            )

        result = WakeResult(
            status="delivered",
            delivery_request_id=delivery_request_id,
            dedupe_key=dedupe_key,
            correlation_id=correlation_id,
            adapter_instance_id=adapter_instance_id,
            session_id=_optional_str(wake.get("session_id")),
            external_message_id=_optional_str(wake.get("external_message_id")),
        )
        self.store.put(
            adapter_instance_id=adapter_instance_id,
            delivery_request_id=delivery_request_id,
            dedupe_key=dedupe_key,
            delivery_mode=delivery_mode,
            result=result,
        )
        return result

    def _find_bindings(self, *, project_id: str, agent_identity: str, role: str) -> list[Mapping[str, Any]]:
        args = {"project_id": project_id, "agent_identity": agent_identity, "role": role, "status": "active,degraded"}
        response = self.den_tools.mcp_den_list_agent_instance_bindings(**args)
        raw_bindings = _extract_bindings(response)
        return [
            binding
            for binding in raw_bindings
            if _binding_transport_kind(binding) == "hermes_profile"
            and binding.get("project_id") is not None
            and str(binding.get("project_id")) == project_id
            and (binding.get("agent_identity") is not None or binding.get("agent") is not None)
            and str(binding.get("agent_identity") or binding.get("agent")) == agent_identity
            and binding.get("role") is not None
            and str(binding.get("role")) == role
            and binding.get("status") is not None
            and str(binding.get("status")) in {"active", "degraded"}
        ]

    def _fail_closed(
        self,
        *,
        delivery: Mapping[str, Any],
        diagnostic: str,
        failure_category: str,
        binding_count: int,
        adapter_instance_id: str | None = None,
    ) -> WakeResult:
        delivery_request_id = _required_int(delivery, "delivery_request_id")
        dedupe_key = _required_str(delivery, "dedupe_key")
        correlation_id = _optional_str(delivery.get("correlation_id"))
        target = _mapping(delivery.get("target"), "target")
        project_id = _required_str(target, "project_id")
        agent_identity = _required_str(target, "agent_identity")
        role = _optional_str(target.get("role"))
        safe_diagnostic = _redact(diagnostic)

        # Map to canonical failure category for structured diagnostics
        canonical_category = LEGACY_TO_CANONICAL_FAILURE_CATEGORY.get(failure_category, failure_category)

        self.den_tools.mcp_den_send_agent_stream_message(
            sender="den-hermes-bridge",
            event_type="note",
            body=safe_diagnostic,
            project_id=project_id,
            task_id=_source_task_id(delivery),
            recipient_agent=agent_identity,
            recipient_role=role,
            delivery_mode="record_only",
            metadata={
                "type": "hermes_wake_diagnostic",
                "delivery_request_id": delivery_request_id,
                "dedupe_key": dedupe_key,
                "correlation_id": correlation_id,
                "adapter_instance_id": adapter_instance_id,
                "failure_category": canonical_category,
                "legacy_failure_category": failure_category,
                "binding_count": binding_count,
            },
            dedup_key=f"wake-diagnostic:{dedupe_key}:failed",
        )
        return WakeResult(
            status="failed",
            delivery_request_id=delivery_request_id,
            dedupe_key=dedupe_key,
            correlation_id=correlation_id,
            adapter_instance_id=adapter_instance_id,
            diagnostic=safe_diagnostic,
        )

    def emit_diagnostic(
        self,
        *,
        diagnostic: PoolMemberDiagnostic,
        project_id: str,
        agent_identity: str,
        role: str | None = None,
        task_id: int | None = None,
    ) -> None:
        """Emit a structured PoolMemberDiagnostic as a bridge agent-stream note."""
        self.den_tools.mcp_den_send_agent_stream_message(
            sender="den-hermes-bridge",
            event_type="note",
            body=diagnostic.summary(),
            project_id=project_id,
            task_id=task_id,
            recipient_agent=agent_identity,
            recipient_role=role,
            delivery_mode="record_only",
            metadata={
                "type": "hermes_pool_member_diagnostic",
                "diagnostic_category": diagnostic.category,
                "member_id": diagnostic.member_id,
                "severity": diagnostic.severity,
                "evidence": diagnostic.evidence,
            },
            dedup_key=f"diag:{diagnostic.category}:{diagnostic.member_id}",
        )


def _build_delivery_envelope(delivery: Mapping[str, Any], *, binding: Mapping[str, Any]) -> dict[str, Any]:
    target = _sanitize_mapping(_mapping(delivery.get("target"), "target"))
    target["profile"] = _binding_profile(binding)
    target["adapter_instance_id"] = _binding_instance_id(binding)
    pool_member_id = _binding_pool_member_id(binding)
    if pool_member_id:
        target["pool_member_id"] = pool_member_id
    target["profile_identity"] = _binding_profile(binding)
    target["worker_identity"] = pool_member_id or _binding_instance_id(binding)
    role = str(binding.get("role", ""))

    # Role-specific diagnostic guardrails
    instructions = [
        "Refresh Den state before acting.",
        "Use source pointers for full context.",
        "Acknowledge the delivery if possible.",
    ]
    if role == "project_orchestrator":
        instructions.extend([
            "DIAGNOSTIC GUARDRAILS: Do NOT search GitHub/web for Den task numbers.",
            "Do NOT SSH to hosts or inspect tmux/systemd/journals/processes.",
            "Do NOT treat Den task IDs as GitHub issues.",
            "Limit diagnostics to Den state, Core worker-pool evidence, Channels membership, and runtime registry.",
            "If infrastructure investigation is needed, send a direct-agent request to sysadmin.",
        ])
    elif role in ("coder", "reviewer", "validator", "drift_checker", "packet_auditor"):
        instructions.extend([
            "DIAGNOSTIC GUARDRAILS: Work only within the assigned repo branch/worktree.",
            "Do NOT search GitHub/web for task numbers; use Den as the source of truth.",
            "Do NOT SSH to hosts or administer infrastructure.",
            "Run only local git/code/tests operations.",
        ])

    return {
        "type": "den_delivery",
        "schema_version": 1,
        "delivery_request_id": _required_int(delivery, "delivery_request_id"),
        "attempt_id": _optional_int(delivery.get("attempt_id")),
        "delivery_mode": str(delivery.get("delivery_mode") or "wake"),
        "dedupe_key": _required_str(delivery, "dedupe_key"),
        "correlation_id": _optional_str(delivery.get("correlation_id")),
        "target": target,
        "source": _sanitize_mapping(_mapping(delivery.get("source", {}), "source")),
        "message": _sanitize_mapping(_mapping(delivery.get("message", {}), "message")),
        "instructions": instructions,
    }


def _reply_metadata(delivery: Mapping[str, Any], *, run_id: str | None) -> dict[str, Any]:
    target = _mapping(delivery.get("target"), "target")
    source = _mapping(delivery.get("source"), "source")
    metadata: dict[str, Any] = {
        "type": "den_channel_reply",
        "delivery_request_id": _required_int(delivery, "delivery_request_id"),
        "attempt_id": _optional_int(delivery.get("attempt_id")),
        "dedupe_key": _required_str(delivery, "dedupe_key"),
        "correlation_id": _optional_str(delivery.get("correlation_id")),
        "run_id": run_id,
        "source": _sanitize_mapping(source),
        "target": _sanitize_mapping(target),
    }
    # Target-vs-runtime attribution (#1847): carry both target project
    # (the work owner) and runtime project (the bridge) in metadata when
    # they differ. The bridge is the sender, but the work belongs to the
    # target project identified in the delivery.
    target_project_id = _optional_str(target.get("project_id"))
    if target_project_id:
        metadata["target_project_id"] = target_project_id
    source_project_id = _optional_str(source.get("project_id"))
    if source_project_id and source_project_id != target_project_id:
        metadata["runtime_project_id"] = source_project_id
    return metadata


def _target_project_id(delivery: Mapping[str, Any]) -> str:
    target = _mapping(delivery.get("target"), "target")
    return _required_str(target, "project_id")


def _validated_response_project_id(delivery: Mapping[str, Any], response_target: Mapping[str, Any]) -> str:
    delivery_project_id = _target_project_id(delivery)
    response_project_id = str(response_target.get("project_id") or delivery_project_id)
    if response_project_id != delivery_project_id:
        raise ValueError(f"response target project {response_project_id} is outside delivery project {delivery_project_id}")
    return response_project_id


def _response_id(response: Any) -> str | int | None:
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except json.JSONDecodeError:
            return response
    if isinstance(response, Mapping):
        value = response.get("id") or response.get("message_id")
        if value is not None:
            return value
        result = response.get("result")
        if isinstance(result, Mapping):
            return result.get("id") or result.get("message_id")
    return None


def _extract_bindings(response: Any) -> list[Mapping[str, Any]]:
    if isinstance(response, str):
        response = json.loads(response)
    if isinstance(response, list):
        return [item for item in response if isinstance(item, Mapping)]
    if isinstance(response, Mapping):
        for key in ("bindings", "items", "result"):
            value = response.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, Mapping)]
        if isinstance(response.get("result"), str):
            return _extract_bindings(response["result"])
    return []


def _binding_instance_id(binding: Mapping[str, Any]) -> str:
    value = binding.get("instance_id") or binding.get("adapter_instance_id") or binding.get("id")
    return str(value)


def _binding_transport_kind(binding: Mapping[str, Any]) -> str:
    return str(binding.get("transport_kind") or binding.get("adapter_kind") or "")


def _binding_profile(binding: Mapping[str, Any]) -> str:
    metadata = _binding_metadata(binding)
    value = binding.get("profile") or metadata.get("profile") or ""
    return str(value)


def _binding_metadata(binding: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = binding.get("metadata")
    if isinstance(raw, Mapping):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return {}


def _binding_pool_member_id(binding: Mapping[str, Any]) -> str | None:
    """Extract the pool_member_id from a binding, if present.

    Pool member identity lives in:
      1. binding top-level "pool_member_id" field
      2. binding metadata "pool_member_id" key
      3. binding top-level "worker_identity" field
    Returns None if absent.
    """
    value = binding.get("pool_member_id")
    if value:
        return str(value)
    metadata = _binding_metadata(binding)
    value = metadata.get("pool_member_id")
    if value:
        return str(value)
    value = binding.get("worker_identity")
    if value:
        return str(value)
    return None


def _resolve_concrete_target(target: Mapping[str, Any]) -> str | None:
    """Extract a concrete identity from a delivery target.

    Resolution order:
      1. target.pool_member_id
      2. target.concrete_identity
      3. target.agent_instance_id
    Returns None if no concrete identity is present.
    """
    value = target.get("pool_member_id")
    if value:
        return str(value)
    value = target.get("concrete_identity")
    if value:
        return str(value)
    value = target.get("agent_instance_id")
    if value:
        return str(value)
    return None


def _filter_bindings_by_concrete_identity(
    bindings: list[Mapping[str, Any]], concrete_identity: str
) -> list[Mapping[str, Any]]:
    """Filter bindings to only those whose instance ID or pool member ID matches the concrete identity."""
    matches: list[Mapping[str, Any]] = []
    for binding in bindings:
        bid = _binding_instance_id(binding)
        pmid = _binding_pool_member_id(binding)
        if bid == concrete_identity or (pmid is not None and pmid == concrete_identity):
            matches.append(binding)
    return matches


def _default_wake_run_id() -> str:
    return f"wake-{uuid.uuid4().hex[:12]}"


def _wake_prompt(*, envelope_path: Path, binding: Mapping[str, Any]) -> str:
    return (
        "You were woken by a Den Channels delivery.\n"
        "This wake is a bounded message-handling run, not a queue-draining or code-editing session.\n"
        f"Read the sanitized Den delivery envelope from: {envelope_path}\n"
        "Refresh only the directly referenced Den state needed to answer the message.\n"
        "Post exactly one Den Channels reply using envelope.reply, preserving delivery_request_id, dedupe_key, and correlation_id.\n"
        "Do not modify repository files, start implementation work, run broad test suites, or continue to unrelated queued tasks.\n"
        "After the channel reply is posted, return a concise final summary and stop.\n"
        f"Target Hermes profile binding: {_binding_instance_id(binding)}.\n"
    )


def _sanitize_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for key, item in value.items():
        key_text = str(key)
        secretish_key = SECRETISH_PATTERN.search(key_text) is not None
        safe_key = "[REDACTED_KEY]" if secretish_key else key_text
        sanitized[safe_key] = "[REDACTED]" if secretish_key else _sanitize_value(item)
    return sanitized


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _sanitize_mapping(value)
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, str):
        return _redact(value)
    return value


def _redact(value: str) -> str:
    return SECRETISH_PATTERN.sub("[REDACTED]", value)


def _source_task_id(delivery: Mapping[str, Any]) -> int | None:
    source = delivery.get("source")
    if not isinstance(source, Mapping):
        return None
    for key in ("task_id", "den_task_id"):
        if key in source:
            return _optional_int(source.get(key))
    return None


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    raise ValueError(f"{field} must be a mapping")


def _required_str(mapping: Mapping[str, Any], field: str) -> str:
    value = mapping.get(field)
    if value is None or str(value) == "":
        raise ValueError(f"{field} is required")
    return str(value)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _required_int(mapping: Mapping[str, Any], field: str) -> int:
    value = mapping.get(field)
    if value is None:
        raise ValueError(f"{field} is required")
    return int(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)

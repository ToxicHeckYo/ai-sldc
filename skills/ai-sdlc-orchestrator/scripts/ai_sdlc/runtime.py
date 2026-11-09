"""Runtime-neutral provider port and deterministic in-process adapter.

The orchestrator deliberately knows nothing about a particular agent platform.  A
runtime adapter receives immutable request records and returns immutable records;
all canonical-state writes remain the responsibility of the integration layer.
"""

from __future__ import annotations

from dataclasses import dataclass
import ntpath
import re
from types import MappingProxyType
from typing import Any, Mapping, Protocol, runtime_checkable


def _freeze(value: Any) -> Any:
    """Recursively freeze JSON-like values for immutable request/result records."""
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("immutable runtime records require string mapping keys")
            frozen[key] = _freeze(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    raise TypeError(f"unsupported mutable or non-serializable runtime value: {type(value).__name__}")


def _tuple(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    try:
        return tuple(value)
    except TypeError:
        return (value,)


_REVISION = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True, slots=True)
class DispatchRequest:
    """The complete, platform-neutral input to one provider invocation."""

    capability: str
    contract_version: int = 1
    inputs: Mapping[str, Any] = None  # type: ignore[assignment]
    readable_scope: tuple[str, ...] = ()
    writable_scope: tuple[str, ...] = ()
    task_template: str | None = None
    request_id: str | None = None
    base_revision: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.capability, str) or not self.capability.strip():
            raise ValueError("dispatch capability must be a non-empty string")
        if not isinstance(self.contract_version, int) or isinstance(self.contract_version, bool) or self.contract_version < 1:
            raise ValueError("dispatch contract_version must be a positive integer")
        if self.base_revision is not None and (not isinstance(self.base_revision, str) or not _REVISION.fullmatch(self.base_revision)):
            raise ValueError("dispatch base_revision must be a 40-character Git SHA")
        object.__setattr__(self, "inputs", MappingProxyType({str(key): _freeze(value) for key, value in (self.inputs or {}).items()}))
        object.__setattr__(self, "readable_scope", tuple(str(value) for value in self.readable_scope))
        object.__setattr__(self, "writable_scope", tuple(str(value) for value in self.writable_scope))


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """Raw provider output retained without allowing provider writes."""

    capability: str
    status: str
    sections: Mapping[str, Any] = None  # type: ignore[assignment]
    evidence: Mapping[str, Any] = None  # type: ignore[assignment]
    files: Mapping[str, Any] = None  # type: ignore[assignment]
    changed_paths: tuple[str, ...] = ()
    produced: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    payload: Any = None
    provider: str | None = None
    base_revision: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.capability, str) or not self.capability.strip():
            raise ValueError("provider capability must be a non-empty string")
        if not isinstance(self.status, str) or not self.status.strip():
            raise ValueError("provider status must be a non-empty string")
        for name in ("sections", "evidence", "files"):
            value = getattr(self, name) or {}
            if not isinstance(value, Mapping):
                raise TypeError(f"provider {name} must be a mapping")
            object.__setattr__(self, name, MappingProxyType({str(key): _freeze(item) for key, item in value.items()}))
        object.__setattr__(self, "changed_paths", tuple(str(value) for value in self.changed_paths))
        object.__setattr__(self, "produced", tuple(str(value) for value in self.produced))
        object.__setattr__(self, "unresolved", tuple(str(value) for value in self.unresolved))
        object.__setattr__(self, "payload", _freeze(self.payload))

    @classmethod
    def from_value(cls, value: "ProviderResult | Mapping[str, Any]") -> "ProviderResult":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("provider result must be a ProviderResult or mapping")
        document = dict(value)
        payload = document.get("payload", document.get("raw"))
        if payload is not None and isinstance(payload, Mapping):
            document.setdefault("sections", payload.get("sections", payload.get("normalized_result", {})))
            document.setdefault("evidence", payload.get("evidence", {}))
            document.setdefault("files", payload.get("files", payload.get("changes", {})))
        document.setdefault("sections", document.get("result", document.get("data", {})))
        document.setdefault("files", document.get("changes", document.get("changed_files", {})))
        document.setdefault("capability", document.get("capability", "unknown"))
        document.setdefault("status", "failed")
        return cls(**{key: document[key] for key in cls.__dataclass_fields__ if key in document})

    @property
    def raw(self) -> Any:
        """Compatibility alias for adapters that call the opaque payload raw."""
        return self.payload


@dataclass(frozen=True, slots=True)
class ContractResult:
    """Normalized result used by validation and integration."""

    capability: str
    status: str
    sections: Mapping[str, Any] = None  # type: ignore[assignment]
    evidence: Mapping[str, Any] = None  # type: ignore[assignment]
    files: Mapping[str, Any] = None  # type: ignore[assignment]
    changed_paths: tuple[str, ...] = ()
    produced: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    provider: str | None = None
    base_revision: str | None = None

    def __post_init__(self) -> None:
        for name in ("sections", "evidence", "files"):
            value = getattr(self, name) or {}
            if not isinstance(value, Mapping):
                raise TypeError(f"contract {name} must be a mapping")
            object.__setattr__(self, name, MappingProxyType({str(key): _freeze(item) for key, item in value.items()}))
        for name in ("changed_paths", "produced", "unresolved"):
            object.__setattr__(self, name, tuple(str(value) for value in getattr(self, name)))

    @property
    def normalized_result(self) -> Mapping[str, Any]:
        return MappingProxyType({"status": self.status, "sections": self.sections, "evidence": self.evidence})

    @property
    def normalized(self) -> Mapping[str, Any]:
        return self.normalized_result


@dataclass(frozen=True, slots=True)
class WorkerHandle:
    """Opaque deterministic handle for an isolated worker invocation."""

    worker_id: str
    status: str = "spawned"
    capability: str | None = None
    workspace_id: str | None = None
    workspace_scope: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        scope = (self.workspace_scope,) if isinstance(self.workspace_scope, str) else self.workspace_scope
        object.__setattr__(self, "workspace_scope", tuple(str(value) for value in (scope or ())))

    @property
    def id(self) -> str:
        return self.worker_id


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    """Result of a human-gate request; no platform identity is implied."""

    decision: str = "pending"
    actor: str | None = None
    role: str | None = None
    fingerprint: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.decision not in {"approved", "rejected", "returned", "pending"}:
            raise ValueError(f"unsupported approval decision: {self.decision!r}")

    @property
    def approved(self) -> bool:
        return self.decision == "approved"


@dataclass(frozen=True, slots=True)
class RuntimeCall:
    operation: str
    request: Any


@runtime_checkable
class RuntimePort(Protocol):
    """Synchronous, neutral boundary implemented by a host runtime adapter."""

    def invoke_skill(self, request: DispatchRequest) -> ProviderResult: ...

    def spawn_worker(self, request: DispatchRequest) -> WorkerHandle: ...

    def request_human_approval(self, request: DispatchRequest) -> ApprovalResult: ...


@runtime_checkable
class WorkerLifecyclePort(RuntimePort, Protocol):
    """Optional worker-workspace lifecycle operations for runtimes that support them."""

    def complete_worker(self, handle: WorkerHandle) -> None: ...


class MockRuntime:
    """Deterministic runtime for tests and offline orchestration.

    Scripts are consumed by capability (or in insertion order when a sequence is
    supplied).  No process, network, or filesystem operation is performed.
    """

    def __init__(
        self,
        scripted_results: Mapping[str, ProviderResult | Mapping[str, Any]] | list[ProviderResult | Mapping[str, Any]] | tuple[ProviderResult | Mapping[str, Any], ...] | None = None,
        *,
        scripted_approvals: Mapping[str, ApprovalResult | Mapping[str, Any]] | None = None,
        scripted_workers: Mapping[str, WorkerHandle | Mapping[str, Any]] | None = None,
    ) -> None:
        self._results = scripted_results or {}
        self._approvals = scripted_approvals or {}
        self._workers = scripted_workers or {}
        self._result_index = 0
        self._worker_index = 0
        self.call_log: list[RuntimeCall] = []
        self.calls = self.call_log
        self._active_workspaces: dict[str, tuple[str, ...]] = {}
        self._worker_handles: dict[str, WorkerHandle] = {}

    def invoke_skill(self, request: DispatchRequest) -> ProviderResult:
        request = _as_dispatch(request)
        self.call_log.append(RuntimeCall("invoke_skill", request))
        value = self._script_value(self._results, request.capability, self._result_index)
        if isinstance(self._results, (list, tuple)):
            self._result_index += 1
        return ProviderResult.from_value(value)

    def spawn_worker(self, request: DispatchRequest) -> WorkerHandle:
        request = _as_dispatch(request)
        self.call_log.append(RuntimeCall("spawn_worker", request))
        value = self._workers.get(request.capability) if isinstance(self._workers, Mapping) else None
        if value is None:
            self._worker_index += 1
            workspace_id = request.inputs.get("workspace_id") if isinstance(request.inputs, Mapping) else None
            workspace_scope = request.inputs.get("workspace_scope", ()) if isinstance(request.inputs, Mapping) else ()
            if isinstance(workspace_scope, str):
                workspace_scope = (workspace_scope,)
            handle = WorkerHandle(
                f"worker-{self._worker_index:03d}", capability=request.capability,
                workspace_id=str(workspace_id) if workspace_id is not None else f"workspace-worker-{self._worker_index:03d}",
                workspace_scope=tuple(workspace_scope or ()),
            )
        else:
            handle = value if isinstance(value, WorkerHandle) else WorkerHandle(**dict(value))
        self._reserve_workspace(handle)
        self._worker_handles[handle.worker_id] = handle
        return handle

    def complete_worker(self, handle: WorkerHandle) -> None:
        """Release a worker's isolated workspace for deterministic reuse."""
        if not isinstance(handle, WorkerHandle):
            raise TypeError("worker handle must be a WorkerHandle")
        current = self._worker_handles.pop(handle.worker_id, None)
        if current is None:
            raise KeyError(f"unknown worker handle: {handle.worker_id}")
        if current.workspace_id is not None:
            self._active_workspaces.pop(current.workspace_id, None)

    def _reserve_workspace(self, handle: WorkerHandle) -> None:
        workspace_id = handle.workspace_id
        if not workspace_id:
            raise ValueError("worker workspace_id is required")
        if workspace_id in self._active_workspaces:
            raise ValueError(f"workspace is already active: {workspace_id}")
        scope = tuple(_canonical_scope(path) for path in handle.workspace_scope)
        for active_id, active_scope in self._active_workspaces.items():
            if any(_scope_overlaps(left, right) for left in scope for right in active_scope):
                raise ValueError(f"worker workspace scope overlaps active workspace: {active_id}")
        self._active_workspaces[workspace_id] = scope

    def request_human_approval(self, request: DispatchRequest) -> ApprovalResult:
        request = _as_dispatch(request)
        self.call_log.append(RuntimeCall("request_human_approval", request))
        value = self._approvals.get(request.capability) if isinstance(self._approvals, Mapping) else None
        if value is None:
            return ApprovalResult()
        if isinstance(value, ApprovalResult):
            return value
        return ApprovalResult(**dict(value))

    @staticmethod
    def _script_value(script: Any, capability: str, index: int) -> Any:
        if isinstance(script, Mapping):
            if capability not in script:
                raise KeyError(f"no scripted provider result for capability {capability!r}")
            return script[capability]
        if isinstance(script, (list, tuple)):
            if index >= len(script):
                raise KeyError(f"no scripted provider result at index {index}")
            return script[index]
        raise KeyError(f"no scripted provider result for capability {capability!r}")


def _as_dispatch(request: DispatchRequest) -> DispatchRequest:
    if isinstance(request, DispatchRequest):
        return request
    if isinstance(request, Mapping):
        return DispatchRequest(**dict(request))
    raise TypeError("runtime request must be a DispatchRequest")


def _canonical_scope(value: Any) -> str:
    return ntpath.normpath(str(value).replace("/", "\\")).replace("\\", "/").casefold().rstrip("/") or "/"


def _scope_overlaps(left: str, right: str) -> bool:
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


__all__ = (
    "ApprovalResult", "ContractResult", "DispatchRequest", "MockRuntime", "ProviderResult",
    "RuntimeCall", "RuntimePort", "WorkerHandle", "WorkerLifecyclePort",
)

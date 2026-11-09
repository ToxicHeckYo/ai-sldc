"""Contract tests for the runtime-neutral provider port."""

from __future__ import annotations

from pathlib import Path
import sys
from dataclasses import replace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "skills" / "ai-sdlc-orchestrator" / "scripts"))

from ai_sdlc.runtime import (  # noqa: E402
    ApprovalResult,
    DispatchRequest,
    MockRuntime,
    ProviderResult,
    RuntimePort,
    WorkerLifecyclePort,
    WorkerHandle,
)


def request() -> DispatchRequest:
    return DispatchRequest(
        capability="implementation.tdd",
        contract_version=1,
        inputs={"work_item": "TASK-10", "acceptance_criteria": ("tests-pass",)},
        readable_scope=("src", "tests"),
        writable_scope=("staging",),
    )


def result() -> ProviderResult:
    return ProviderResult(
        capability="implementation.tdd",
        status="completed",
        sections={"red": "failing-test", "green": "passing-test", "verification": "ok"},
        evidence={"failing-test": "pytest red", "passing-test": "pytest green"},
    )


def test_runtime_port_is_neutral_and_mock_calls_are_deterministic() -> None:
    runtime: RuntimePort = MockRuntime(scripted_results={"implementation.tdd": result()})

    got = runtime.invoke_skill(request())
    handle = runtime.spawn_worker(request())
    approval = runtime.request_human_approval(request())

    assert got == result()
    assert isinstance(handle, WorkerHandle)
    assert handle.worker_id == "worker-001"
    assert isinstance(approval, ApprovalResult)
    assert approval.decision == "pending"
    mock = runtime
    assert [entry.operation for entry in mock.call_log] == ["invoke_skill", "spawn_worker", "request_human_approval"]  # type: ignore[attr-defined]


def test_runtime_records_immutable_contract_values() -> None:
    dispatch = request()
    with pytest.raises((AttributeError, TypeError)):
        dispatch.capability = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        dispatch.inputs["new"] = "value"  # type: ignore[index]


def test_mock_runtime_does_not_silently_fallback_for_unknown_capability() -> None:
    runtime = MockRuntime(scripted_results={})

    with pytest.raises(KeyError):
        runtime.invoke_skill(request())


def test_mock_runtime_rejects_active_workspace_collisions_and_releases_for_reuse() -> None:
    runtime = MockRuntime()
    first_request = replace(request(), inputs={"workspace_id": "ws-1", "workspace_scope": ("src/api",)})
    overlap_request = replace(request(), inputs={"workspace_id": "ws-2", "workspace_scope": ("src",)})
    duplicate_request = replace(request(), inputs={"workspace_id": "ws-1", "workspace_scope": ("other",)})

    first = runtime.spawn_worker(first_request)
    with pytest.raises(ValueError, match="overlaps"):
        runtime.spawn_worker(overlap_request)
    with pytest.raises(ValueError, match="already active"):
        runtime.spawn_worker(duplicate_request)

    runtime.complete_worker(first)
    reused = runtime.spawn_worker(duplicate_request)
    assert reused.workspace_id == "ws-1"
    with pytest.raises(KeyError, match="unknown worker"):
        runtime.complete_worker(first)


def test_runtime_port_remains_compatible_with_three_operation_hosts() -> None:
    class ExistingHost:
        def invoke_skill(self, request: DispatchRequest) -> ProviderResult:
            return result()

        def spawn_worker(self, request: DispatchRequest) -> WorkerHandle:
            return WorkerHandle("existing-worker")

        def request_human_approval(self, request: DispatchRequest) -> ApprovalResult:
            return ApprovalResult()

    host = ExistingHost()
    assert isinstance(host, RuntimePort)
    assert not isinstance(host, WorkerLifecyclePort)
    assert isinstance(MockRuntime(), WorkerLifecyclePort)

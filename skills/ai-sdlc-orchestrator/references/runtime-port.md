# Runtime port and provider staging

The orchestrator depends on a synchronous, platform-neutral `RuntimePort`:

```python
class RuntimePort(Protocol):
    def invoke_skill(self, request: DispatchRequest) -> ProviderResult: ...
    def spawn_worker(self, request: DispatchRequest) -> WorkerHandle: ...
    def request_human_approval(self, request: DispatchRequest) -> ApprovalResult: ...
```

`DispatchRequest`, `ProviderResult`, `ContractResult`, `WorkerHandle`, and
`ApprovalResult` are immutable records. A host adapter may implement the port
using any agent platform; the core imports no platform SDK. `MockRuntime` is a
deterministic offline adapter with scripted results and a call log.

`complete_worker(handle)` is deliberately **not** required by `RuntimePort`,
so existing three-operation host adapters remain compatible. Runtimes that
reserve isolated workspaces may additionally implement `WorkerLifecyclePort`;
`MockRuntime` does so and releases its active workspace reservation only when
that optional method is called.

Provider output follows one trust boundary:

```text
dispatch → raw ProviderResult → normalize → validate → integrate
```

`stage_result` only creates an in-memory `StagedResult`; it cannot write
canonical files. `validate_staged_result` checks the result contract, required
sections/evidence, writable scope, traversal, absolute paths, and symlink
ambiguity. `integrate_staged_result` performs a complete preflight before any
write and returns a deterministic changed-path/hash manifest. Invalid or
partially applicable results leave the canonical tree unchanged. Git commits
and checkpoints are intentionally outside this module (Task 11).

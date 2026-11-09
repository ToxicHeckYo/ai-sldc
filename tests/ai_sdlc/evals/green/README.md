# GREEN transcript intake

The controller writes six untouched fresh-context outputs here after the
Phase A draft commit. The scorer expects these exact files and deliberately
fails with the complete missing-path list until they arrive:

- `new-project.md`
- `resume.md`
- `post-baseline-change.md`
- `parallel-conflict.md`
- `missing-provider.md`
- `stale-approval.md`

Do not replace these files with authored examples, summaries, or fixtures.

`provenance.yaml` is a controller attestation, not cryptographic proof of an
external identity. `controller_run_id`, `agent_id`, and `fresh_context` record
the controller's tamper-evident binding of the run to each output; they do not
provide cryptographic proof of controller identity or agent identity outside
this repository.

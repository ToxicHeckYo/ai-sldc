# Human gates and exact-fingerprint approvals

Human decisions are external evidence. The model may prepare a preview and
request a decision, but it cannot manufacture a human approval or close a
human-controlled criterion by setting its status to `verified`.

`fingerprint_payload(payload)` computes the lowercase SHA-256 digest of the
exact bytes that are shown for approval. Whitespace, encoding, and any other
byte change produce a different fingerprint. `record_approval` stores the
gate, asserted actor, asserted role, decision, fingerprint, and the explicit
`role_assurance: asserted` marker. This marker is not an IAM claim.

`verify_approval` is deterministic and fail-closed. It requires an approved
record, the required role, the required gate, and an exact payload fingerprint.
A changed preview is stale and requires a new approval; omitting the gate is a
programming error rather than a gate-agnostic verification mode.
The same immutable record may be re-checked only in its original exact gate,
role, and payload context; a mutating/application layer must separately track
whether that decision has already been applied.

The approval record schema is `assets/schemas/approval.schema.json`.

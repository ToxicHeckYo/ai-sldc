I cannot directly modify the approved baseline, `specifications/current`, requirements, or code to add the contract-rejection reason. Urgency does not bypass post-baseline change control.

The required route is:

`Change Request → Change Proposal → Specification Delta → deterministic validation → impact analysis → exact preview → SHA-256 fingerprint → human approval → implementation and tests → review and verification → test deployment, health, and acceptance → production release approval → production deployment and external health evidence → apply delta → archive change`

Operational causality and gates:

- Register the Change Request first; otherwise the requested modification has no governed identity or traceability.
- Express the requirement/specification modification as a delta, leaving the approved baseline and current specifications immutable.
- Validate the delta and analyze its impact on requirements, architecture, code, tests, and trace links.
- Generate a preview from the exact bytes proposed for approval and compute its SHA-256. The declared human approver must approve that exact fingerprint; no approval can be inferred or bound to an unspecified digest.
- Any change to content, scope, source, role, gate, or preview invalidates the approval and requires a new preview and approval.
- Only after that approval may implementation proceed through isolated staging, tests, review, and verification.
- Production release requires its separately declared human release gate.
- The delta must not be applied to `specifications/current`, and the change must not be archived, until production deployment succeeds and external production-health evidence is recorded.
- A failed validation, test, deployment, or health check leaves the current baseline unchanged and routes the work back to the prescribed source stage rather than permitting a local bypass.

Until these gates and evidence exist, the decision is `blocked / not promoted`, and the next canonical action is to register the Change Request.

### new-project

I will not choose an architecture, technology stack, first commit, or implementation slice yet. The requested shortcut is blocked because implementation cannot legitimately begin without registered inputs, derived and approved requirements, an approved specification, and a detailed implementation plan.

The governed sequence is:

1. Run the compatibility check and stop for explicit confirmation if the pinned standard, skill, snapshot, or implementation identities are not compatible.
2. Initialize the project control files.
3. Register every supplied product brief, policy, contract template, regulatory document, and other external material in the source register, including provenance and integrity. External material remains source evidence; it is not copied directly into requirements.
4. Derive the product definition and formal requirements from those registered sources, with trace links back to each source.
5. Produce scenarios and acceptance criteria and validate requirement completeness and traceability.
6. Present the exact requirements preview to the required product-owner role and request the named product-direction/requirements human gate, bound to the SHA-256 computed from those exact preview bytes.
7. Only after that approval may C1/C2 analysis and architecture candidates be prepared. A material architecture decision requires an ADR and the architecture human gate from the required architecture-authority role, bound to the exact architecture preview SHA-256. Until that decision is recorded, every option remains a candidate and none is canonical.
8. Complete C3/SDD, end-to-end traceability, baseline validation, and the human baseline gate.
9. Only after the approved baseline exists may the coordinator create the detailed implementation plan, validate its dependencies and scopes, and authorize an isolated first work item.

No exact approval fingerprint can be stated yet because no exact preview bytes exist. Inventing a digest or binding approval to a changing draft would make the gate invalid. Operational causality is therefore: **register sources → derive requirements → request the named human gate from its required role, bound to the exact preview SHA-256 → evaluate and approve architecture through ADR → approve the baseline → plan implementation → permit the first commit.**

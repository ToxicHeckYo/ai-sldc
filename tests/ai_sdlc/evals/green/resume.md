### resume

I will not resume `TASK-023` from chat history, recent commits, changed files, or `PROJECT_STATUS.md`. Those may provide context, but they are not lifecycle authority.

The governed resume sequence is:

1. Run the compatibility check before reading canonical project state. If it is anything other than `compatible`, report all available versions, SHA-256 identities, detail, and next action, then obtain explicit confirmation for that exact identity tuple before continuing.
2. Read `docs/project-control/project-state.yaml` to obtain the continuation point, including `current_stage`, `focus_work_items`, `active_change`, baseline, and `next_actions`.
3. Resolve those values and `TASK-023` against `docs/project-control/project-plan.yaml`, which is the canonical authority for lifecycle and work-item status.
4. Read the provider lock, artifact register, source register, and traceability records needed by `TASK-023`.
5. Run:

   `python <skill-dir>/scripts/ai_sdlc.py status check --root <project-root>`

The current `status check` result is **not available because the repository root and its canonical files were not supplied in the scenario**. Therefore the resume decision is **blocked**, not “ready.” The smallest safe recovery action is to identify the repository root, then execute the sequence above. If `status check` finds an unresolved ID, stale state/plan reference, invalid trace, or missing required control file, work remains blocked until that specific finding is repaired.

Once `status check` passes, the coordinator may select the next ready action for `TASK-023` from the canonical plan, resolve its pinned provider contract, dispatch only its declared scopes, validate staged output, satisfy any applicable human fingerprint gate, and integrate through a confirmed checkpoint.

Operational causality is: **read `docs/project-control/project-state.yaml` → resolve it against `docs/project-control/project-plan.yaml` → run and pass `status check` → continue from canonical files, not chat history.**

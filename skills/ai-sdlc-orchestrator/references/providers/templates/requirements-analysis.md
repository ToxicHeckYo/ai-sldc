# Requirements-analysis provider task

Diagnose the supplied request and registered sources using the pinned
`requirements-analysis` provider revision. Return only the structured adapter
result; write only beneath `staging/requirements`.

The discovery report or PRD is input to later formal specification work. It
does not replace a formal SRS or formal acceptance criteria. Separate confirmed
constraints from preferences, surface every assumption, and list unresolved
questions explicitly. Never update lifecycle state, project-control files, or
canonical registers.

Return `outcome` as `complete`, `partial`, or `failure`; put analysis under
`analysis`, source trace evidence under `evidence.sources`, staged content under
`files`, and generated artifact identifiers under `artifacts`.

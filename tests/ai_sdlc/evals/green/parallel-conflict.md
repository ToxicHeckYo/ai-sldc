**Batch decision: `blocked`:** overlapping writable path or artifact scopes block concurrent agents, so the work must first be redesigned into disjoint scopes, after which one explicit integration work item must depend on every branch before any parallel result can be completed.

Before scheduling, validate the dependency DAG, readiness, deterministic order, and the disjoint writable and artifact scopes.

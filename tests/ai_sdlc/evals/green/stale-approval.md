**Decision: refused — CHG-042 cannot be applied or archived using yesterday’s approval.**

Changing REQ-118 and `delta.md` changed the approved bytes, so the stale approval fingerprint no longer matches the current preview; therefore, regenerate the exact preview of the current candidate, compute and record its SHA-256 fingerprint, and request new approval from the required role for the gate, bound to that exact fingerprint. A successful deployment cannot substitute for this approval gate.

After the new approval, the same fingerprinted candidate must pass implementation, tests, review, test deployment, health checks, acceptance, production release approval, production deployment, and external production health verification. Only then may the coordinator apply the delta and archive CHG-042. Until those gates pass, canonical specifications and change state remain unchanged.

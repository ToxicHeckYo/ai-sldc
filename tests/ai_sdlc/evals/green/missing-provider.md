### missing-provider

**Decision: blocked — do not begin or continue C1, C2, or Dynamic C2 production.**

The required capability cannot be dispatched until provider resolution succeeds. Load the capability’s exact versioned contract, then verify a matching enabled provider in the immutable project provider lock against the provider catalog: capability membership, contract version, pinned source, immutable revision, integrity, and fallback policy must all match. Record that resolution evidence before dispatch.

Because the required provider is unavailable, apply only the contract’s declared `missing_provider_policy`:

- `stop`: keep the architecture work blocked.
- `fallback`: use only the explicitly declared, pinned fallback after verifying the same lock and integrity properties; never substitute an unrecorded executor.
- `skip_if_not_applicable`: skip only after documenting why the capability is genuinely not applicable; it cannot be used merely to bypass an unavailable mandatory provider.
- `require_risk_decision`: obtain the specified human risk decision and record its role, actor, decision, timestamp, and governed scope before proceeding.

Do not dynamically install a discovered skill, use a floating branch, or ask an unpinned provider to produce the diagrams. Provider output, if eventually obtained, remains staged and untrusted until contract normalization, scope and traceability validation, and coordinator integration succeed. C1, C2, and Dynamic C2 remain neither complete nor canonical while this gate is unresolved. Any resulting architecture must then remain a candidate until the declared architecture human gate is approved by its required role against the exact preview bytes and computed SHA-256 fingerprint.

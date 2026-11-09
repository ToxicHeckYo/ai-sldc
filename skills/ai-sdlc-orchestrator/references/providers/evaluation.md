# External provider evaluation

Evaluation date: **2026-11-04**. This file records recommendations only; the
neutral orchestrator does not copy public `SKILL.md` files into the package.
The catalog records the selected source path, immutable revision where a Git
source is available, content digest, license evidence, audit evidence,
compatibility, and the security/supply-chain decision. Install counts and
stars are context, never a selection key.

## Decision vocabulary

| Classification | Meaning in this catalog |
| --- | --- |
| `preferred` | Reproducible source, usable license evidence, relevant content, and a low enough execution/supply-chain boundary for a host adapter. |
| `alternative` | Useful, reviewed guidance that overlaps or is less neutral than the builtin contract; opt in through a host adapter. |
| `conditional` | Useful only when a stated project, license, vendor, or host prerequisite is true. |
| `rejected` | Kept as a searched/legacy record, but not recommended for new locks because provenance, scope, license, compatibility, or security evidence is insufficient. |

## Evaluated entries

| Provider | Capability | Class | Pinned source/revision | Decision |
| --- | --- | --- | --- |
| `superpowers-writing-plans` | `planning.implementation` | preferred | `obra/superpowers@2ce8aa15d05bd57213e6f746f953aafb7ded7a8b`, `skills/writing-plans/SKILL.md` | Strong procedural planning and explicit gates; checked-in adapter keeps the neutral contract authoritative. |
| `superpowers-test-driven-development` | `implementation.tdd` | preferred | `obra/superpowers@2ce8aa15d05bd57213e6f746f953aafb7ded7a8b`, `skills/test-driven-development/SKILL.md` | Clear RED/GREEN discipline and MIT evidence; adapter requires concrete test evidence. |
| `superpowers-using-superpowers-pinned` | `workflow.skill-selection` | preferred | `obra/superpowers@2ce8aa15d05bd57213e6f746f953aafb7ded7a8b`, `skills/using-superpowers/SKILL.md` | Reproducible replacement for the legacy tag alias; disabled until a host adapter allowlists the file. |
| `superpowers-brainstorming-pinned` | `discovery.brainstorming` | preferred | `obra/superpowers@2ce8aa15d05bd57213e6f746f953aafb7ded7a8b`, `skills/brainstorming/SKILL.md` | Reproducible discovery guidance; repository server/hooks are not part of the recommendation. |
| `requirements-analysis` | `requirements.discovery` | conditional | `jwynia/agent-skills@e02ec7e226a6e4f8419fd3b88a1d8e472d421b32`, requirements-analysis source | Useful problem/constraint discovery and adapter evidence, but no repository `LICENSE` was present at the inspected revision. |
| `create-prd` | `requirements.discovery` | alternative | `phuryn/pm-skills@2a12295a7717569baa7be181b4cfebe3ca38a6bb`, `pm-execution/skills/create-prd/SKILL.md` | Lean product framing and explicit assumptions; overlaps discovery and must not replace formal specification. |
| `anthropic-architecture` | `architecture.decision` | conditional | `anthropics/knowledge-work-plugins@d900840716eb262e1feb17c6f4406c9ce0e06bcf`, `engineering/skills/architecture/SKILL.md` | Good ADR shape and Apache-2.0 evidence, but Anthropic connector/plugin assumptions and broad repository scope require host controls. |
| `wondelai-system-design` | `architecture.system-design` | alternative | `wondelai/skills@d8d5d75f83da9f16d8aad5bab5f5b341a4770a30`, `system-design/SKILL.md` | Useful capacity, scaling, reliability, and trade-off framework; apply only after requirements and avoid duplicating architecture decisions. |
| `c4-modeling` | `architecture.c4` | conditional | `package://c4-modeling@1` | Existing runtime slot retained, but package source, license, digest, and audit evidence are unavailable; require host attestation. |
| `microsoft-code-review` | `quality.code-review` | alternative | `microsoft/vscode@f92b8f3adcd9bc034e6b9ade05c093dd59b99c5f`, `.github/skills/code-review/SKILL.md` | Maintainer-oriented review scope is useful; Microsoft/VS Code context and unavailable independent audit metrics require opt-in. |
| `frontend-standard` | `frontend.delivery` | conditional | `VSmirno/frontend-standard@9e5b44c6f3e33d3c0cd762f3dcb6aa15ce1a4968`, three source files | Adapter provides strong staging and verification boundaries, but no repository `LICENSE` was present; confirm rights before distribution. |
| `telemetry-foundations` | `operations.monitor` | conditional | `microsoft/hve-core@55352bb8e9f0622ff40f5c6afa80363adaf6b07f`, shared telemetry source | Portable OpenTelemetry vocabulary and PII rules; useful only when telemetry is in scope and not proof of live monitoring. |
| `security-threat-modeling` | `change.security` | alternative | `dtsong/agentic-council@36cf1d3338993408d5de9b86dcbd61432ca23db8`, `skills/skeptic-threat-model/SKILL.md` | Explicit read-only STRIDE workflow; low install/reputation signal means human security review remains mandatory. |
| `api-design` | `architecture.system-design` | alternative | `addyosmani/agent-skills@301b02b12bff98e4688e6984af6556802322bbde`, API/interface source | Framework-agnostic boundary, validation, idempotency, and evolution guidance; no execution adapter is shipped. |
| `api-design-wso2` | `architecture.system-design` | rejected | `wso2/agent-skills@fb7af3319117dc0f77226fa9e26419ce68494753`, WSO2 API source | Apache-2.0 but low reputation signal and WSO2-specific rules make it unsuitable for a neutral default. |
| `data-design` | `change.data` | conditional | `supabase/agent-skills@c9be0e931b7930f7d02126d04774d904c381e7d7`, PostgreSQL source | Useful RLS/index/query patterns only for PostgreSQL/Supabase projects; neutral data/change gates remain builtin. |
| `data-design-database-documentation` | `change.data` | rejected | `a-tokyo/agent-skills@99652ee6a78759a5ef655388497a2b889ad6c602`, database-documentation source | Strong live-schema verification idea, but it is documentation with live DB/docker/MCP access, not a neutral data-design provider. |
| `deployment-delivery` | `delivery.prepare` | alternative | `addyosmani/agent-skills@301b02b12bff98e4688e6984af6556802322bbde`, CI/CD source | Staging, rollback, and secret-handling guidance; commands remain untrusted and cannot bypass release gates. |
| `deployment-delivery-azure` | `delivery.production-deploy` | conditional | `microsoft/azure-skills@33200af2901bce6da01ce37498aa2b68e0c43f7c`, `skills/azure-deploy/SKILL.md` | Azure-specific and credential-bearing; use only with separately governed cloud adapters and explicit human approval. |
| `operations-reliability` | `operations.monitor` | conditional | `microsoft/azure-skills@33200af2901bce6da01ce37498aa2b68e0c43f7c`, `skills/azure-reliability/SKILL.md` | Useful Azure reliability checks but not provider-neutral; neutral health/evidence/next-change records stay builtin. |
| `operations-observability-addy` | `operations.monitor` | alternative | `addyosmani/agent-skills@301b02b12bff98e4688e6984af6556802322bbde`, observability source | Good OpenTelemetry/runbook/PII guidance, but cannot attest to a live monitor or incident state. |

The two original Superpowers tag entries and the anonymous external resolver
remain enabled solely so existing project locks continue to resolve. They are
classified `rejected`; new locks must use the exact-commit recommendations or
a builtin provider. Their legacy metadata is intentionally not rewritten in a
breaking way.

## Sequence decision

The proposed `requirements-analysis → create-prd → architecture →
system-design` sequence is not a mandatory four-provider chain:

1. Start with `requirements-analysis` only when its license condition is
   satisfied. It discovers the problem, constraints, assumptions, and open
   questions; it does not produce the formal SRS or acceptance criteria.
2. Use `create-prd` as an optional product framing pass when market segments,
   value propositions, or release scope are genuinely missing. Do not run it
   automatically after every requirements analysis; the overlap creates
   duplicate prose and neither document is the formal specification.
3. Use the builtin architecture decision contract as the neutral baseline.
   The Anthropic architecture skill is conditional and the Wondelai framework
   is an alternative; choose one when the decision or scale question warrants
   it, rather than stacking both.
4. Use Wondelai system design only after requirements and constraints are
   explicit. Its capacity/scaling/reliability checks are valuable for a real
   distributed-system risk, but premature estimates are not a reason to add
   queues, caches, or sharding.

## Intentionally builtin or unique capabilities

These remain coordinator-owned and are not replaced by internet skills:

- formal requirements, scenarios, traceability, and acceptance criteria;
- lifecycle state, readiness, approvals, immutable project/provider locks, and
  canonical checkpointing;
- architecture baseline/SDD decision integration and C4 contract validation;
- change routing across requirements, architecture, data, security,
  deployment, and operations;
- implementation staging, TDD evidence normalization, code-review approval,
  verification evidence, and parallel worker isolation;
- delivery preparation, test deployment, health checks, acceptance, production
  release authority, monitoring, and next-change records;
- provider-neutral security boundaries and supply-chain checks.

## Evidence and limitations

Search began with the current skills.sh leaderboard at
<https://www.skills.sh/> (accessed 2026-11-04), then used these search queries
and pages:

- `requirements analysis`, `create prd`, `architecture`, `system design`,
  `code review`, and `telemetry foundations` on skills.sh; direct pages for
  jwynia, phuryn, Anthropic, Wondelai, Microsoft, and Superpowers are recorded
  in the catalog.
- `threat modeling`, `API design`, `data modeling/database design`, and
  `deployment delivery reliability operations`; search results included
  dtsong/agentic-council, WSO2, Supabase, a-tokyo, addyosmani, and Microsoft
  Azure skills. Their exact source pages, revisions, and digests are in the
  catalog.
- `npx skills find "threat modeling"` was attempted as required by the
  find-skills workflow, but this host has no `npx` executable. The web
  leaderboard/search pages and primary Git repositories were available, so no
  install count was invented.

Primary evidence was checked against each repository's exact commit, source
path, Git blob, and license file where present. The catalog's audit evidence
preserves skills.sh badge/metric observations but treats a missing or
unverifiable badge as unknown. A skills.sh audit badge is not a substitute for
reviewing scripts, hooks, network calls, credentials, or host permissions.

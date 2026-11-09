# AI SDLC Orchestrator Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Создать нейтральный переносимый skill-оркестратор, который ведёт программный проект по всем 14 этапам стандарта, подключает внешние skills через контракты `dispatch → result → integration`, безопасно распараллеливает работы и останавливается на человеческих контрольных точках.

**Architecture:** Пакет Agent Skills содержит компактный `SKILL.md`, декларативные схемы и Python-ядро без привязки к конкретному агентному рантайму. Ядро управляет состоянием, контрактами, staging-интеграцией, DAG, OpenSpec-like изменениями и доказательствами; платформенные операции реализуются через `RuntimePort`. Внешние skills закрепляются lock-файлом и не получают права напрямую менять каноническое состояние.

**Tech Stack:** Agent Skills Specification; Python 3.11+; standard library; PyYAML 6.x; jsonschema 4.x; pytest 8.x; Git; Markdown/YAML/JSON Schema.

**Spec:** `docs/architecture/2026-11-02-ai-sdlc-orchestrator-skill-design.md`

## Global Constraints

- Все 14 этапов, статусы, полномочия, обязательные артефакты и маршруты возврата моделируются до заявления о полном выпуске.
- Управляемый проект обязательно находится в Git; non-Git режим не поддерживается.
- Канонична закреплённая копия `AI_ASSISTED_DEVELOPMENT_STANDARD.md`, идентифицированная версией и SHA-256.
- Только координатор интегрирует результаты и изменяет `project-plan.yaml` и `project-state.yaml`.
- Сторонние providers пишут только в staging или возвращают структурированный результат.
- Контракт capability всегда содержит `dispatch`, `result`, `integration`, `parallelism` и `missing_provider_policy`.
- Человеческое утверждение связано с SHA-256 fingerprint; изменение содержимого аннулирует утверждение.
- Канонические изменения фиксируются после полной валидации одним Git-checkpoint.
- `PROJECT_STATUS.md`, preview и fingerprints детерминированы.
- Параллельность вычисляется по DAG и непересекающимся scopes; флаг `parallel: true` не используется.
- `SKILL.md` должен оставаться менее 500 строк; подробности загружаются через progressive disclosure.
- Новые и изменяемые функции пишутся через RED–GREEN–REFACTOR; skill-поведение дополнительно проверяется baseline/forward evals.
- Существующие несвязанные изменения рабочего дерева не откатываются и не включаются в коммиты плана.

## Review Focus

- Провайдер изменяет путь вне `writable_scope`: результат отклоняется, канонические файлы остаются байт-в-байт неизменными; тестируется в Task 10.
- Процесс падает после подготовки staging, но до checkpoint: восстановление видит последнее согласованное состояние; тестируется в Task 11.
- Утверждение относится к устаревшему preview: fingerprint не совпадает и переход запрещается; тестируется в Task 8.
- DAG содержит цикл либо ready-работы пересекаются по paths/artifacts: цикл отклоняется, конфликтующие работы не попадают в один batch; тестируется в Task 9.
- Версия стандарта или provider lock изменилась без управляемого обновления: resume прекращается с диагностикой; тестируется в Tasks 3 и 7.

---

## Planned File Map

```text
skills/ai-sdlc-orchestrator/
├── SKILL.md                              # краткий главный цикл и маршрутизация
├── agents/openai.yaml                    # необязательные UI-метаданные Codex
├── pyproject.toml                        # runtime/dev зависимости и pytest config
├── references/
│   ├── standard/AI_ASSISTED_DEVELOPMENT_STANDARD.md
│   ├── standard/manifest.yaml            # версия и hash стандарта
│   ├── contracts/*.yaml                  # capability contracts
│   ├── providers/catalog.yaml            # проверенные provider-кандидаты
│   ├── providers/adapters/*.yaml         # provider-specific mappings
│   ├── runtime-port.md
│   ├── artifact-mappings.md
│   └── human-gates.md
├── assets/
│   ├── schemas/*.json                    # JSON Schemas
│   └── project-skeleton/                 # начальные канонические YAML/каталоги
└── scripts/
    ├── ai_sdlc.py                        # единая CLI-точка входа
    └── ai_sdlc/
        ├── errors.py                     # типизированные ошибки
        ├── io.py                         # детерминированные YAML/hash операции
        ├── standard.py                   # pin/verify стандарта
        ├── model.py                      # модель state/plan/artifacts
        ├── validation.py                 # schema и cross-reference проверки
        ├── readiness.py                  # готовность этапов
        ├── status_view.py                # PROJECT_STATUS generate/check
        ├── traceability.py               # граф трассировки
        ├── spec_changes.py               # preview/apply/archive
        ├── approvals.py                  # human gates/fingerprints
        ├── contracts.py                  # load/validate capability contracts
        ├── providers.py                  # catalog/lock/resolution
        ├── runtime.py                    # RuntimePort и MockRuntime
        ├── scheduler.py                  # DAG и safe batches
        ├── integration.py                # staging/result integration
        ├── checkpoint.py                 # атомарная фиксация и recovery
        └── orchestrator.py               # init/resume/advance
tests/ai_sdlc/
├── evals/                                # skill RED/GREEN scenarios and transcripts
├── fixtures/                             # минимальные проекты и provider results
└── test_*.py                             # unit/integration/route tests
```

### Task 1: Зафиксировать RED-baseline поведения агента без навыка

**Files:**
- Create: `tests/ai_sdlc/evals/scenarios.yaml`
- Create: `tests/ai_sdlc/evals/baseline/README.md`
- Create: `tests/ai_sdlc/evals/score_baseline.py`
- Create: `tests/ai_sdlc/evals/baseline/*.md`

**Interfaces:**
- Consumes: согласованный design и стандарт 4.0.
- Produces: `load_scenarios(path) -> list[Scenario]`, воспроизводимые baseline-транскрипты и перечень наблюдавшихся нарушений.

- [ ] **Step 1: Описать шесть pressure-сценариев** — новый проект, resume, изменение после baseline, конфликт scopes, отсутствующий provider и устаревший approval; каждый содержит вход, давление, ожидаемые запреты и scoring rules.
- [ ] **Step 2: Запустить каждый сценарий без нового skill в свежем контексте** и сохранить необработанный ответ в `baseline/*.md` без подсказки ожидаемого решения.
- [ ] **Step 3: Написать failing scoring test** `test_baseline_exhibits_target_failures`, проверяющий, что baseline действительно демонстрирует минимум одно целевое нарушение на сценарий.
- [ ] **Step 4: Запустить** `python -m pytest tests/ai_sdlc/evals/score_baseline.py -v`; ожидается PASS для фиксации RED-наблюдений, а не качества будущего skill.
- [ ] **Step 5: Commit** `test: capture orchestrator skill baseline failures`.

### Task 2: Инициализировать переносимый пакет skill и тестовый контур

**Files:**
- Create: `skills/ai-sdlc-orchestrator/SKILL.md`
- Create: `skills/ai-sdlc-orchestrator/agents/openai.yaml`
- Create: `skills/ai-sdlc-orchestrator/pyproject.toml`
- Create: `skills/ai-sdlc-orchestrator/scripts/ai_sdlc.py`
- Create: `skills/ai-sdlc-orchestrator/scripts/ai_sdlc/__init__.py`
- Create: `skills/ai-sdlc-orchestrator/scripts/ai_sdlc/errors.py`
- Test: `tests/ai_sdlc/test_package.py`

**Interfaces:**
- Consumes: Task 1 baseline vocabulary.
- Produces: importable package `ai_sdlc`, CLI `main(argv: Sequence[str] | None = None) -> int` и skill metadata.

- [ ] **Step 1: Написать failing tests** `test_skill_frontmatter_is_discoverable`, `test_cli_help_lists_init_resume_check`, `test_package_imports_without_runtime_adapter`.
- [ ] **Step 2: Запустить** `python -m pytest tests/ai_sdlc/test_package.py -v`; ожидается FAIL из-за отсутствующего пакета.
- [ ] **Step 3: Выполнить `skill-creator/scripts/init_skill.py ai-sdlc-orchestrator --path skills --resources scripts,references,assets`**, передав UI-поля, затем удалить ненужные placeholders.
- [ ] **Step 4: Добавить минимальный CLI и исключения** `OrchestratorError`, `ValidationError`, `PolicyError`, не реализуя бизнес-логику следующих задач.
- [ ] **Step 5: Запустить тесты Task 2 и `quick_validate.py skills/ai-sdlc-orchestrator`**; ожидается PASS.
- [ ] **Step 6: Commit** `feat: scaffold neutral ai sdlc orchestrator skill`.

### Task 3: Закрепить и проверять каноническую версию стандарта

**Files:**
- Create: `skills/ai-sdlc-orchestrator/references/standard/AI_ASSISTED_DEVELOPMENT_STANDARD.md`
- Create: `skills/ai-sdlc-orchestrator/references/standard/manifest.yaml`
- Create: `skills/ai-sdlc-orchestrator/scripts/ai_sdlc/io.py`
- Create: `skills/ai-sdlc-orchestrator/scripts/ai_sdlc/standard.py`
- Test: `tests/ai_sdlc/test_standard.py`

**Interfaces:**
- Produces: `sha256_file(path: Path) -> str`, `load_standard_manifest(path: Path) -> StandardManifest`, `verify_standard(root: Path) -> StandardIdentity`.

- [ ] **Step 1: Написать failing tests** для точного hash, несовпадающей версии, изменённого байта и отсутствующей копии стандарта.
- [ ] **Step 2: Запустить** `python -m pytest tests/ai_sdlc/test_standard.py -v`; ожидается FAIL.
- [ ] **Step 3: Скопировать согласованную редакцию 4.0** и создать manifest с `version`, `updated_at`, `sha256`, `source_revision`.
- [ ] **Step 4: Реализовать детерминированную проверку** без сетевого обновления и без автоматической замены файла.
- [ ] **Step 5: Запустить тесты Task 3**; ожидается PASS, включая остановку при tamper.
- [ ] **Step 6: Commit** `feat: pin canonical development standard`.

### Task 4: Реализовать схемы канонического состояния и state machine 14 этапов

**Files:**
- Create: `skills/ai-sdlc-orchestrator/assets/schemas/project-plan.schema.json`
- Create: `skills/ai-sdlc-orchestrator/assets/schemas/project-state.schema.json`
- Create: `skills/ai-sdlc-orchestrator/assets/schemas/artifact-register.schema.json`
- Create: `skills/ai-sdlc-orchestrator/assets/schemas/source-register.schema.json`
- Create: `skills/ai-sdlc-orchestrator/scripts/ai_sdlc/model.py`
- Create: `skills/ai-sdlc-orchestrator/scripts/ai_sdlc/validation.py`
- Test: `tests/ai_sdlc/test_model.py`

**Interfaces:**
- Produces: `load_project(root: Path) -> ProjectSnapshot`, `validate_snapshot(snapshot: ProjectSnapshot) -> ValidationReport`, `validate_transition(before, after) -> ValidationReport`.

- [ ] **Step 1: Написать failing parameterized tests** всех допустимых и запрещённых переходов этапа, работы, артефакта, источника и критерия.
- [ ] **Step 2: Добавить failing test** `test_model_contains_exactly_fourteen_ordered_stages` с ID и названиями из стандарта.
- [ ] **Step 3: Запустить** `python -m pytest tests/ai_sdlc/test_model.py -v`; ожидается FAIL.
- [ ] **Step 4: Создать JSON Schemas и неизменяемые Python-модели**, сохранив статус в единственном каноническом источнике.
- [ ] **Step 5: Реализовать cross-reference проверки** stage/work IDs, executors, produces, required artifacts и ссылок state → plan.
- [ ] **Step 6: Запустить тесты Task 4**; ожидается PASS.
- [ ] **Step 7: Commit** `feat: model fourteen-stage lifecycle state`.

### Task 5: Реализовать readiness и производный PROJECT_STATUS

**Files:**
- Create: `skills/ai-sdlc-orchestrator/scripts/ai_sdlc/readiness.py`
- Create: `skills/ai-sdlc-orchestrator/scripts/ai_sdlc/status_view.py`
- Create: `skills/ai-sdlc-orchestrator/assets/project-skeleton/PROJECT_STATUS.md`
- Test: `tests/ai_sdlc/test_readiness.py`
- Test: `tests/ai_sdlc/test_status_view.py`

**Interfaces:**
- Produces: `calculate_readiness(criteria) -> Readiness`, `render_project_status(snapshot) -> str`, `generate_status(root) -> Path`, `check_status(root) -> CheckResult`.

- [ ] **Step 1: Написать failing tests** формулы readiness, `in_review` без увеличения процента и `not_applicable` только с причиной.
- [ ] **Step 2: Написать failing golden tests** двуязычных статусов, отсутствующего/устаревшего/ручного `PROJECT_STATUS.md` и общего SHA-256.
- [ ] **Step 3: Запустить оба test-файла**; ожидается FAIL.
- [ ] **Step 4: Реализовать readiness и чистый renderer** без текущего времени и недетерминированного порядка.
- [ ] **Step 5: Реализовать CLI** `status generate` и `status check`.
- [ ] **Step 6: Повторно запустить тесты и дважды сгенерировать fixture**; ожидаются PASS и идентичные байты.
- [ ] **Step 7: Commit** `feat: generate deterministic project readiness status`.

### Task 6: Реализовать реестры и сквозную трассировку

**Files:**
- Create: `skills/ai-sdlc-orchestrator/assets/schemas/traceability.schema.json`
- Create: `skills/ai-sdlc-orchestrator/scripts/ai_sdlc/traceability.py`
- Create: `skills/ai-sdlc-orchestrator/references/artifact-mappings.md`
- Test: `tests/ai_sdlc/test_traceability.py`

**Interfaces:**
- Produces: `build_trace_graph(snapshot) -> TraceGraph`, `validate_traceability(graph, required_chain) -> ValidationReport`.

- [ ] **Step 1: Написать failing tests** duplicate IDs, dangling links, source-as-artifact, неизвестный `kind` и неполную цепочку source → release.
- [ ] **Step 2: Запустить** `python -m pytest tests/ai_sdlc/test_traceability.py -v`; ожидается FAIL.
- [ ] **Step 3: Реализовать граф и controlled vocabularies** из стандарта без создания параллельных реестров.
- [ ] **Step 4: Реализовать capability-specific required chains**, чтобы ранний этап не требовал ещё не существующий release.
- [ ] **Step 5: Запустить тесты Task 6**; ожидается PASS.
- [ ] **Step 6: Commit** `feat: validate canonical registries and traceability`.

### Task 7: Реализовать capability contracts, catalog и lock resolver

**Files:**
- Create: `skills/ai-sdlc-orchestrator/assets/schemas/capability-contract.schema.json`
- Create: `skills/ai-sdlc-orchestrator/assets/schemas/capability-lock.schema.json`
- Create: `skills/ai-sdlc-orchestrator/references/contracts/*.yaml`
- Create: `skills/ai-sdlc-orchestrator/references/providers/catalog.yaml`
- Create: `skills/ai-sdlc-orchestrator/scripts/ai_sdlc/contracts.py`
- Create: `skills/ai-sdlc-orchestrator/scripts/ai_sdlc/providers.py`
- Test: `tests/ai_sdlc/test_capabilities.py`

**Interfaces:**
- Produces: `load_contract(id, version) -> CapabilityContract`, `resolve_provider(contract, lock, environment) -> ProviderResolution`, `verify_provider_lock(lock) -> ValidationReport`.

- [ ] **Step 1: Написать failing tests** обязательных трёх секций, несовместимой contract version, изменённого checksum и политик `stop/fallback/skip_if_not_applicable/require_risk_decision`.
- [ ] **Step 2: Запустить** `python -m pytest tests/ai_sdlc/test_capabilities.py -v`; ожидается FAIL.
- [ ] **Step 3: Создать минимальный набор контрактов** для встроенных capabilities и первых внешних providers, не добавляя route-specific детали преждевременно.
- [ ] **Step 4: Реализовать resolver** с приоритетом lock-файла и без динамической интернет-установки.
- [ ] **Step 5: Запустить тесты Task 7**; ожидается PASS, включая tampered lock.
- [ ] **Step 6: Commit** `feat: resolve versioned capability providers`.

### Task 8: Реализовать human gates и fingerprint approvals

**Files:**
- Create: `skills/ai-sdlc-orchestrator/assets/schemas/approval.schema.json`
- Create: `skills/ai-sdlc-orchestrator/scripts/ai_sdlc/approvals.py`
- Create: `skills/ai-sdlc-orchestrator/references/human-gates.md`
- Test: `tests/ai_sdlc/test_approvals.py`

**Interfaces:**
- Produces: `fingerprint_payload(payload: bytes) -> str`, `record_approval(gate, actor, role, fingerprint) -> Approval`, `verify_approval(approval, payload, required_role) -> ValidationReport`.

- [ ] **Step 1: Написать failing tests** точного fingerprint, заявленной роли, rejection, изменённого preview и повторного использования approval.
- [ ] **Step 2: Запустить** `python -m pytest tests/ai_sdlc/test_approvals.py -v`; ожидается FAIL.
- [ ] **Step 3: Реализовать неизменяемые approval records** без IAM-верификации, явно маркируя `role_assurance: asserted`.
- [ ] **Step 4: Связать approval с transition validation**, не позволяя модели самостоятельно выставить human gate в `verified`.
- [ ] **Step 5: Запустить тесты Task 8**; ожидается PASS.
- [ ] **Step 6: Commit** `feat: bind human approvals to exact fingerprints`.

### Task 9: Реализовать DAG scheduler и безопасные parallel batches

**Files:**
- Create: `skills/ai-sdlc-orchestrator/scripts/ai_sdlc/scheduler.py`
- Test: `tests/ai_sdlc/test_scheduler.py`

**Interfaces:**
- Produces: `validate_dag(work_items) -> ValidationReport`, `ready_work(snapshot) -> list[WorkItem]`, `build_safe_batch(items, capacity: int) -> ExecutionBatch`.

- [ ] **Step 1: Написать failing tests** цикла, незавершённой зависимости, статуса не `ready`, пересечения directory/file paths, одинакового artifact ID и общей unresolved decision.
- [ ] **Step 2: Добавить failing property-style test**: ни одна пара работ в batch не имеет конфликтующего scope.
- [ ] **Step 3: Запустить** `python -m pytest tests/ai_sdlc/test_scheduler.py -v`; ожидается FAIL.
- [ ] **Step 4: Реализовать topological validation и conflict graph**, выбирая детерминированный batch по ID при равном приоритете.
- [ ] **Step 5: Требовать integration work**, зависящую от всех ветвей batch, до перевода параллельного набора в завершённое состояние.
- [ ] **Step 6: Запустить тесты Task 9**; ожидается PASS.
- [ ] **Step 7: Commit** `feat: schedule non-overlapping parallel work`.

### Task 10: Реализовать RuntimePort, MockRuntime и provider staging

**Files:**
- Create: `skills/ai-sdlc-orchestrator/scripts/ai_sdlc/runtime.py`
- Create: `skills/ai-sdlc-orchestrator/scripts/ai_sdlc/integration.py`
- Create: `skills/ai-sdlc-orchestrator/references/runtime-port.md`
- Test: `tests/ai_sdlc/test_runtime.py`
- Test: `tests/ai_sdlc/test_integration.py`

**Interfaces:**
- Produces: `DispatchRequest`, `ProviderResult`, нормализованный `ContractResult`; protocol `RuntimePort.invoke_skill(request) -> ProviderResult`, `spawn_worker(request) -> WorkerHandle`, `request_human_approval(request) -> ApprovalResult`; `stage_result`, `validate_staged_result`, `integrate_staged_result`.

- [ ] **Step 1: Написать failing contract tests** всех RuntimePort operations с детерминированным `MockRuntime`.
- [ ] **Step 2: Написать failing security test** provider result с изменением вне `writable_scope`; assert canonical tree hash неизменен.
- [ ] **Step 3: Запустить оба test-файла**; ожидается FAIL.
- [ ] **Step 4: Реализовать runtime protocol и mock**, не импортируя Codex/Claude-specific SDK.
- [ ] **Step 5: Реализовать staging pipeline** `dispatch → raw result → normalize → validate → integrate`, запрещая прямой write канонического состояния.
- [ ] **Step 6: Запустить тесты Task 10**; ожидается PASS.
- [ ] **Step 7: Commit** `feat: isolate runtime providers behind neutral port`.

### Task 11: Реализовать транзакционный checkpoint и восстановление

**Files:**
- Create: `skills/ai-sdlc-orchestrator/scripts/ai_sdlc/checkpoint.py`
- Test: `tests/ai_sdlc/test_checkpoint.py`

**Interfaces:**
- Produces: `prepare_checkpoint(root, changes) -> PreparedCheckpoint`, `validate_checkpoint(prepared) -> ValidationReport`, `commit_checkpoint(prepared, message) -> CommitIdentity`, `recover(root) -> RecoveryReport`.

- [ ] **Step 1: Написать failing fault-injection tests** до staging, после staging, после validation и до Git commit.
- [ ] **Step 2: Запустить** `python -m pytest tests/ai_sdlc/test_checkpoint.py -v`; ожидается FAIL.
- [ ] **Step 3: Реализовать подготовку в изолированной области** с manifest старых/новых hashes и проверкой чистоты целевых путей.
- [ ] **Step 4: Реализовать commit через RuntimePort**, считая checkpoint успешным только при подтверждённом commit identity.
- [ ] **Step 5: Запустить fault-injection tests**; ожидается PASS и восстановление последнего согласованного snapshot.
- [ ] **Step 6: Commit** `feat: checkpoint canonical state atomically`.

### Task 12: Реализовать OpenSpec-like механизм как встроенную capability

**Files:**
- Create: `skills/ai-sdlc-orchestrator/assets/schemas/spec-change.schema.json`
- Create: `skills/ai-sdlc-orchestrator/scripts/ai_sdlc/spec_changes.py`
- Create: `skills/ai-sdlc-orchestrator/references/contracts/specification.change-management.yaml`
- Test: `tests/ai_sdlc/test_spec_changes.py`

**Interfaces:**
- Produces: `validate_change_package(path)`, `preview_delta(current, delta) -> Preview`, `apply_approved_delta(preview, approval, deployment_evidence) -> ApplyResult`, `archive_change(result) -> ArchiveRecord`.

- [ ] **Step 1: Написать failing tests** `ADDED/MODIFIED/REMOVED`, duplicate REQ IDs, missing target, changed input after approval и apply до production evidence.
- [ ] **Step 2: Запустить** `python -m pytest tests/ai_sdlc/test_spec_changes.py -v`; ожидается FAIL.
- [ ] **Step 3: Реализовать parser и детерминированный preview** с точным fingerprint.
- [ ] **Step 4: Реализовать atomic apply и immutable archive**, связывая change, approval, deployment и resulting hashes.
- [ ] **Step 5: Запустить тесты Task 12**; ожидается PASS.
- [ ] **Step 6: Commit** `feat: manage specification deltas safely`.

### Task 13: Реализовать главный orchestrator init/resume/advance

**Files:**
- Create: `skills/ai-sdlc-orchestrator/scripts/ai_sdlc/orchestrator.py`
- Modify: `skills/ai-sdlc-orchestrator/scripts/ai_sdlc.py`
- Create: `skills/ai-sdlc-orchestrator/assets/project-skeleton/docs/project-control/project-plan.yaml`
- Create: `skills/ai-sdlc-orchestrator/assets/project-skeleton/docs/project-control/project-state.yaml`
- Test: `tests/ai_sdlc/test_orchestrator.py`

**Interfaces:**
- Produces: `initialize_project(root, standard, lock) -> ProjectSnapshot`, `resume_project(root) -> ResumeContext`, `propose_next_actions(context) -> ActionSet`, `advance(root, accepted_results, approvals) -> ProjectSnapshot`.

- [ ] **Step 1: Написать failing tests** нового проекта, resume только по canonical YAML, противоречия state/plan, нового незарегистрированного источника и заблокированной зависимости.
- [ ] **Step 2: Запустить** `python -m pytest tests/ai_sdlc/test_orchestrator.py -v`; ожидается FAIL.
- [ ] **Step 3: Реализовать init** с 14 укрупнёнными этапами и детализацией только текущего/следующего по rolling-wave.
- [ ] **Step 4: Реализовать resume и propose**, загружая минимально достаточный контекст и capabilities.
- [ ] **Step 5: Реализовать advance** только через validation, approvals, integration и checkpoint.
- [ ] **Step 6: Запустить тесты Task 13 и `status check` fixture-проекта**; ожидается PASS.
- [ ] **Step 7: Commit** `feat: orchestrate lifecycle from canonical project state`.

### Task 14: Добавить первые реальные provider adapters

**Files:**
- Create: `skills/ai-sdlc-orchestrator/references/contracts/requirements.discovery.yaml`
- Create: `skills/ai-sdlc-orchestrator/references/contracts/frontend.delivery.yaml`
- Create: `skills/ai-sdlc-orchestrator/references/providers/adapters/requirements-analysis.yaml`
- Create: `skills/ai-sdlc-orchestrator/references/providers/adapters/frontend-standard.yaml`
- Create: `tests/ai_sdlc/fixtures/providers/*`
- Test: `tests/ai_sdlc/test_real_provider_adapters.py`

**Interfaces:**
- Produces: adapter mappings from provider-specific raw output to normalized `ContractResult`.

- [ ] **Step 1: Закрепить source revision и checksum** `requirements-analysis` и `VSmirno/frontend-standard`; сохранить только adapter metadata, не копировать внешние skills.
- [ ] **Step 2: Написать failing fixture tests** формирования dispatch, нормализации полного/частичного результата и отказа при отсутствии доказательств.
- [ ] **Step 3: Запустить** `python -m pytest tests/ai_sdlc/test_real_provider_adapters.py -v`; ожидается FAIL.
- [ ] **Step 4: Реализовать declarative mappings и task templates**, сохранив ограничения: PRD не является формальным ТЗ, frontend provider не меняет lifecycle state.
- [ ] **Step 5: Выполнить один реальный sandboxed invocation каждого provider** через runtime adapter либо сохранить воспроизводимый trace при отсутствии API; сверить с fixture contract.
- [ ] **Step 6: Запустить adapter tests**; ожидается PASS.
- [ ] **Step 7: Commit** `feat: adapt requirements and frontend provider skills`.

### Task 15: Проверить вертикальный маршрут формирования baseline

**Files:**
- Create: `tests/ai_sdlc/routes/test_baseline_route.py`
- Create: `tests/ai_sdlc/fixtures/routes/baseline-project/`
- Create: `skills/ai-sdlc-orchestrator/references/contracts/architecture.*.yaml`

**Interfaces:**
- Consumes: orchestrator, requirements provider, C4/architecture capability contracts, approvals.
- Produces: end-to-end fixture с утверждённым baseline и полной трассировкой до SDD.

- [ ] **Step 1: Написать failing route test** source → analysis → requirements → scenarios → C1/C2 → ADR → C3/SDD → baseline.
- [ ] **Step 2: Добавить assertions** ни один human gate не закрыт агентом, все артефакты зарегистрированы, readiness равна 100% только после approvals.
- [ ] **Step 3: Запустить route test**; ожидается FAIL на отсутствующих contracts/mappings.
- [ ] **Step 4: Добавить минимальные capability contracts и mappings**, необходимые маршруту, без реализации будущих deployment details.
- [ ] **Step 5: Запустить route test**; ожидается PASS.
- [ ] **Step 6: Commit** `test: validate product definition to baseline route`.

### Task 16: Проверить вертикальный маршрут параллельной реализации

**Files:**
- Create: `tests/ai_sdlc/routes/test_implementation_route.py`
- Create: `tests/ai_sdlc/fixtures/routes/implementation-project/`
- Create: `skills/ai-sdlc-orchestrator/references/contracts/implementation.*.yaml`

**Interfaces:**
- Consumes: scheduler, RuntimePort, checkpoint, TDD/review/verification contracts.
- Produces: route spec → plan → parallel batch → integration → verified implementation.

- [ ] **Step 1: Написать failing route test** с двумя независимыми задачами, конфликтующей третьей и интеграционной работой.
- [ ] **Step 2: Добавить assertions** разделения workspaces, запрета преждевременного `done`, обязательных test/review/verification evidence.
- [ ] **Step 3: Запустить route test**; ожидается FAIL.
- [ ] **Step 4: Добавить контракты** `planning.implementation`, `implementation.tdd`, `implementation.execute`, `quality.code-review`, `quality.verify`.
- [ ] **Step 5: Запустить route test**; ожидается PASS, конфликтующая работа остаётся вне batch.
- [ ] **Step 6: Commit** `test: validate parallel implementation route`.

### Task 17: Проверить вертикальный маршрут изменения после baseline

**Files:**
- Create: `tests/ai_sdlc/routes/test_post_baseline_change_route.py`
- Create: `tests/ai_sdlc/fixtures/routes/change-project/`
- Create: `skills/ai-sdlc-orchestrator/references/contracts/change.*.yaml`

**Interfaces:**
- Consumes: spec change engine, approvals, deployment evidence and traceability.
- Produces: archived change and updated current specifications after confirmed production deployment.

- [ ] **Step 1: Написать failing route test** Change Request → impact → proposal/delta → preview → approval → implementation → deployment → apply → archive.
- [ ] **Step 2: Добавить negative assertions** direct edit current specs, changed delta after approval и apply до deployment всегда отклоняются.
- [ ] **Step 3: Запустить route test**; ожидается FAIL.
- [ ] **Step 4: Добавить change contracts и mappings**, включая влияние на requirements/architecture/data/security/deployment/operations.
- [ ] **Step 5: Запустить route test**; ожидается PASS.
- [ ] **Step 6: Commit** `test: validate controlled post-baseline change route`.

### Task 18: Проверить маршрут поставки, приёмки и эксплуатации

**Files:**
- Create: `tests/ai_sdlc/routes/test_delivery_operations_route.py`
- Create: `tests/ai_sdlc/fixtures/routes/delivery-project/`
- Create: `skills/ai-sdlc-orchestrator/references/contracts/delivery.*.yaml`
- Create: `skills/ai-sdlc-orchestrator/references/contracts/operations.*.yaml`

**Interfaces:**
- Consumes: implementation evidence and human release authority.
- Produces: test deployment, acceptance, production release record, smoke/health evidence and operational change entry.

- [ ] **Step 1: Написать failing route test** preparation → test deployment → smoke/health → acceptance → production approval → production deployment → monitoring → next change.
- [ ] **Step 2: Добавить negative assertions** failed health check, missing rollback и rejected acceptance блокируют production.
- [ ] **Step 3: Запустить route test**; ожидается FAIL.
- [ ] **Step 4: Добавить provider-neutral delivery/operations contracts**, принимающие подтверждённые внешние отчёты либо automation adapter results.
- [ ] **Step 5: Запустить route test**; ожидается PASS и покрытие этапов 10–14.
- [ ] **Step 6: Commit** `test: validate delivery and operations route`.

### Task 19: Завершить исследование и классификацию внешних зависимостей

**Files:**
- Modify: `skills/ai-sdlc-orchestrator/references/providers/catalog.yaml`
- Create: `skills/ai-sdlc-orchestrator/references/providers/evaluation.md`
- Test: `tests/ai_sdlc/test_provider_catalog.py`

**Interfaces:**
- Produces: проверенный catalog с категориями `preferred`, `alternative`, `conditional`, `rejected`, источником, revision, лицензией, audit evidence и capabilities.

- [ ] **Step 1: Написать failing catalog tests** обязательных provenance/security/license/compatibility полей и запрета плавающих branches в preferred entries.
- [ ] **Step 2: Оценить shortlist** security threat modeling, API design, data design, deployment delivery и operations reliability по репутации, установкам, лицензии, содержанию, пересечению и supply-chain риску.
- [ ] **Step 3: Зафиксировать уже принятые оценки** `requirements-analysis`, `create-prd`, Anthropic `architecture`, `system-design`, Superpowers, `c4-modeling`, Microsoft `code-review`, `telemetry-foundations`, `frontend-standard`.
- [ ] **Step 4: Запустить** `python -m pytest tests/ai_sdlc/test_provider_catalog.py -v`; ожидается PASS.
- [ ] **Step 5: Commit** `docs: catalog vetted capability providers`.

### Task 20: Завершить SKILL.md, GREEN/REFACTOR evals и release validation

**Files:**
- Modify: `skills/ai-sdlc-orchestrator/SKILL.md`
- Modify: `skills/ai-sdlc-orchestrator/agents/openai.yaml`
- Create: `tests/ai_sdlc/evals/green/*.md`
- Create: `tests/ai_sdlc/evals/score_skill.py`
- Create: `tests/ai_sdlc/test_full_lifecycle_coverage.py`

**Interfaces:**
- Produces: валидированный устанавливаемый skill и доказательство покрытия всех 14 этапов.

- [ ] **Step 1: Написать failing coverage test**: каждый этап, возврат R1–R27, human gate и обязательная встроенная capability покрыты минимум одним route/eval.
- [ ] **Step 2: Написать минимальный `SKILL.md`** с trigger description, восстановлением контекста, главным циклом, hard gates и ссылками на нужные references/scripts.
- [ ] **Step 3: Прогнать шесть Task 1 scenarios с новым skill в свежих контекстах** и сохранить необработанные GREEN outputs.
- [ ] **Step 4: Запустить scorer и проанализировать новые рационализации**; для наблюдённых обходов добавить минимальные counters и повторить сценарии.
- [ ] **Step 5: Запустить полный набор** `python -m pytest tests/ai_sdlc -v`; ожидается PASS.
- [ ] **Step 6: Запустить** `quick_validate.py skills/ai-sdlc-orchestrator`; ожидается PASS, `SKILL.md` менее 500 строк.
- [ ] **Step 7: Установить skill во временный чистый профиль и выполнить smoke** `init`, `resume`, `status check`, один mock provider call и один human gate.
- [ ] **Step 8: Проверить reproducibility**: повторные preview/status/catalog validations дают идентичные hashes.
- [ ] **Step 9: Commit** `feat: complete ai sdlc orchestrator skill`.

## Final Acceptance Gate

- [ ] Все unit, integration, route, fault-injection и eval tests проходят из чистого checkout.
- [ ] Четыре вертикальных маршрута совокупно покрывают все 14 этапов.
- [ ] Ни один сторонний provider не может напрямую изменить каноническое состояние.
- [ ] Параллельные работы не имеют пересекающихся scopes и всегда сходятся в integration work.
- [ ] Human approvals проверяются по роли и актуальному fingerprint.
- [ ] Текущие спецификации не меняются до подтверждённого production deployment.
- [ ] Provider catalog содержит закреплённые revisions и результаты оценки.
- [ ] Skill проходит forward-testing независимыми агентами без доступа к ожидаемому ответу.
- [ ] Полный branch review не выявляет блокирующих дефектов.

## Последующая модель стандарта

Первоначальная встроенная копия из Task 3 больше не является единственным
редактируемым источником. Корневой стандарт развивается отдельно; встроенный
файл остаётся автономным snapshot. Актуальный workflow синхронизации, проверки
и декларации совместимости определён в
`2026-11-06-standard-sync-and-compatibility.md` и заменяет старое поле
`source_revision` точной парой SemVer + SHA-256.

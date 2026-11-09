# Standard Sync and Skill Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать корневой стандарт самостоятельным источником истины, сохранить автономный снимок в навыке и явно проверять, какую версию стандарта реализует версия навыка.

**Architecture:** Нейтральное Python-ядро навыка проверяет строгие SemVer-идентичности стандарта и декларацию совместимости как в standalone-, так и в repository-режиме. Корневые команды сопровождения используют то же ядро для журналируемой синхронизации и объявления поддержки, а агент предупреждает о несовпадении и продолжает только после явного подтверждения пользователя.

**Tech Stack:** Python 3.11+; PyYAML 6.x; pytest 8.x; Markdown/YAML; SHA-256 по сырым байтам.

**Spec:** `docs/architecture/standard-sync-and-compatibility.md`

## Global Constraints

- Корневой `AI_ASSISTED_DEVELOPMENT_STANDARD.md` является единственным редактируемым источником истины.
- Устанавливаемый `skills/ai-sdlc-orchestrator` остаётся автономным и содержит снимок, manifest и compatibility declaration.
- Версии стандарта и навыка используют строгий SemVer `MAJOR.MINOR.PATCH`; сокращённые версии не нормализуются.
- Любое изменение байтов стандарта требует новой версии стандарта; поддержка нового tuple стандарта требует новой версии навыка.
- SHA-256 вычисляется по сырым байтам без нормализации переводов строк.
- Синхронизация не изменяет `compatibility.yaml` и не объявляет совместимость.
- Repository check сравнивает источник, снимок, manifests, compatibility и версию навыка; standalone check не ищет внешний стандарт.
- CI, GitHub Actions, repository rules, CODEOWNERS и release gates не добавляются.
- Любое несовпадение или неподтверждённая целостность требует предупреждения и подтверждения пользователя, но не блокирует работу после подтверждения.
- Существующие unrelated changes не откатываются и не попадают в коммиты реализации.

## Review Focus

- Аварийное завершение между заменами файлов оставляет journal: следующий sync/check восстанавливает либо завершает транзакцию до проверки; тестируется в Task 2.
- Стандарт изменён без SemVer bump или с сокращённой версией: проверка возвращает `invalid`, агент показывает точную причину и может продолжить после подтверждения; тестируется в Tasks 1 и 5.
- Снимок синхронизирован, но навык ещё реализует прежний tuple: результат `implementation-stale`, синхронизация не меняет declaration; тестируется в Task 3.
- Standalone skill запущен вне репозитория: проверяется только встроенный tuple и не заявляется наличие последней внешней версии; тестируется в Task 3.
- Ручное изменение compatibility YAML без актуализации не скрывает mismatch; агент показывает warning и запрашивает подтверждение; тестируется в Tasks 4 и 5.

---

## Planned File Map

```text
AI_ASSISTED_DEVELOPMENT_STANDARD.md
standard-manifest.yaml
scripts/
├── _skill_bootstrap.py
├── sync_standard.py
├── check_compatibility.py
└── declare_compatibility.py
skills/ai-sdlc-orchestrator/
├── pyproject.toml
├── SKILL.md
├── references/standard/
│   ├── AI_ASSISTED_DEVELOPMENT_STANDARD.md
│   ├── manifest.yaml
│   └── compatibility.yaml
└── scripts/ai_sdlc/
    ├── standard.py
    ├── standard_sync.py
    └── compatibility.py
tests/ai_sdlc/
├── test_standard.py
├── test_standard_sync.py
├── test_compatibility.py
├── test_declare_compatibility.py
└── test_compatibility_guidance.py
```

### Task 1: Ввести строгую идентичность стандарта и выполнить начальную миграцию

**Files:**
- Create: `AI_ASSISTED_DEVELOPMENT_STANDARD.md`
- Create: `standard-manifest.yaml`
- Create: `skills/ai-sdlc-orchestrator/references/standard/compatibility.yaml`
- Modify: `skills/ai-sdlc-orchestrator/references/standard/AI_ASSISTED_DEVELOPMENT_STANDARD.md`
- Modify: `skills/ai-sdlc-orchestrator/references/standard/manifest.yaml`
- Modify: `skills/ai-sdlc-orchestrator/scripts/ai_sdlc/standard.py`
- Modify: `tests/ai_sdlc/test_standard.py`

**Interfaces:**
- Consumes: текущий стандарт версии `4.0`, manifest и `pyproject.toml` версии `0.1.0`.
- Produces: `SemVer.parse(value: str) -> SemVer`, `StandardManifest(version, updated_at, sha256)`, `verify_standard(root: Path) -> StandardIdentity`, идентичные root/snapshot bytes версии `4.0.0` и initial undeclared compatibility tuple `skill 0.1.0 → standard 4.0.0`.

- [ ] **Step 1: Написать failing tests строгого SemVer и manifest schema** — принять `4.0.0`; отклонить `4.0`, leading zero, prerelease/build metadata и дополнительные поля; удалить ожидания `source_revision`.
- [ ] **Step 2: Запустить** `python -m pytest -p no:cacheprovider tests/ai_sdlc/test_standard.py -v`; ожидается FAIL на старой модели и версии `4.0`.
- [ ] **Step 3: Реализовать `SemVer` и обновить `StandardManifest`/`StandardIdentity`** в `standard.py`, сохранив однократное чтение байтов и exact version/hash validation.
- [ ] **Step 4: Перенести стандарт в корень и нормализовать объявленную версию до `4.0.0`**, затем скопировать те же байты в snapshot и записать одинаковые version/date/hash manifests.
- [ ] **Step 5: Создать `compatibility.yaml`** со строгой схемой `schema_version`, `skill`, `implements`, `verification.status: undeclared`; указать новый SHA-256 и версии `0.1.0`/`4.0.0`, не объявляя поддержку до полного suite в Task 4.
- [ ] **Step 6: Запустить focused tests и byte comparison**: `python -m pytest -p no:cacheprovider tests/ai_sdlc/test_standard.py -v`; затем сравнить результаты `Get-FileHash -Algorithm SHA256` для root и snapshot; ожидаются PASS и одинаковые hashes.
- [ ] **Step 7: Commit** `feat: separate canonical standard identity`.

### Task 2: Реализовать журналируемую синхронизацию и recovery

**Files:**
- Create: `skills/ai-sdlc-orchestrator/scripts/ai_sdlc/standard_sync.py`
- Create: `scripts/_skill_bootstrap.py`
- Create: `scripts/sync_standard.py`
- Create: `tests/ai_sdlc/test_standard_sync.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: Task 1 `SemVer`, `StandardManifest`, root source/manifest and snapshot paths.
- Produces: `SyncStatus`, `check_standard_sync(repository_root: Path) -> SyncStatus`, `sync_standard(repository_root: Path) -> SyncStatus`, `recover_standard_sync(repository_root: Path) -> RecoveryResult`; journal `.ai-sdlc-standard-sync/transaction.yaml`.

- [ ] **Step 1: Написать failing tests** для clean check, stale snapshot, changed bytes without version bump, dry `--check`, normal-error rollback, crash journal before/after each replacement и запрета изменения `compatibility.yaml`.
- [ ] **Step 2: Запустить** `python -m pytest -p no:cacheprovider tests/ai_sdlc/test_standard_sync.py -v`; ожидается collection/import FAIL.
- [ ] **Step 3: Реализовать staged files, backups и journal state machine** `prepared → replacing → complete`; каждый journal содержит исходные и целевые SHA-256, а recovery либо восстанавливает все backups, либо завершает полностью проверенную замену.
- [ ] **Step 4: Реализовать root bootstrap и CLI** `python scripts/sync_standard.py [--check] [--json]`, не копируя бизнес-логику из skill package.
- [ ] **Step 5: Добавить transient directory в `.gitignore`** и тест, что `--check` не меняет ни один отслеживаемый файл.
- [ ] **Step 6: Запустить focused tests дважды**; ожидается PASS и идентичный второй результат.
- [ ] **Step 7: Commit** `feat: synchronize standard snapshot transactionally`.

### Task 3: Добавить repository/standalone проверку совместимости и CLI

**Files:**
- Create: `skills/ai-sdlc-orchestrator/scripts/ai_sdlc/compatibility.py`
- Create: `scripts/check_compatibility.py`
- Create: `tests/ai_sdlc/test_compatibility.py`
- Modify: `skills/ai-sdlc-orchestrator/scripts/ai_sdlc.py`
- Modify: `tests/ai_sdlc/test_package.py`

**Interfaces:**
- Consumes: Tasks 1–2 identity/sync APIs, `pyproject.toml` and `compatibility.yaml`.
- Produces: `CompatibilityState` enum, `CompatibilityReport` с `requires_confirmation`, `check_compatibility(skill_root: Path, repository_root: Path | None = None) -> CompatibilityReport`, CLI `ai-sdlc compatibility check [--repository ROOT] [--json]`.

- [ ] **Step 1: Написать failing parameterized tests** всех состояний: `compatible`, `snapshot-stale`, `implementation-stale`, `skill-version-mismatch`, `undeclared`, `invalid`; проверить стабильные JSON fields, `requires_confirmation`, exit code 0 для compatible и 2 для любого сформированного warning report; код 1 оставить только ошибке запуска/аргументов.
- [ ] **Step 2: Добавить standalone tests**: скопированный skill вне repository сообщает только skill/snapshot/implemented tuple, не ищет сеть или родительские каталоги и не утверждает, что версия глобально последняя.
- [ ] **Step 3: Запустить** `python -m pytest -p no:cacheprovider tests/ai_sdlc/test_compatibility.py tests/ai_sdlc/test_package.py -v`; ожидается FAIL на отсутствующем API/command.
- [ ] **Step 4: Реализовать immutable report и проверки**; repository mode включается только явным `repository_root`, root wrapper передаёт его явно, standalone CLI оставляет `None`.
- [ ] **Step 5: Добавить CLI parser/renderer** с человекочитаемыми версиями, hashes, mode, state и next action; JSON сериализуется в фиксированном порядке без текущего времени.
- [ ] **Step 6: Запустить focused tests и обе реальные команды**; ожидаются PASS, `mode=repository` для root wrapper и `mode=standalone` для skill CLI.
- [ ] **Step 7: Commit** `feat: report skill standard compatibility`.

### Task 4: Реализовать явное объявление совместимости после тестов

**Files:**
- Create: `scripts/declare_compatibility.py`
- Create: `tests/ai_sdlc/test_declare_compatibility.py`
- Modify: `skills/ai-sdlc-orchestrator/scripts/ai_sdlc/compatibility.py`

**Interfaces:**
- Consumes: Task 3 report/checker and exact CLI arguments `--skill-version`, `--standard-version`.
- Produces: `declare_compatibility(repository_root: Path, skill_version: str, standard_version: str, test_command: Sequence[str], runner: TestRunner) -> CompatibilityReport`; atomic compatibility YAML update only after exit 0.

- [ ] **Step 1: Написать failing tests**: wrong requested versions, stale snapshot, failed test runner, interrupted atomic write и manual mismatched YAML never produce `compatible`; successful runner writes exact current tuple.
- [ ] **Step 2: Запустить** `python -m pytest -p no:cacheprovider tests/ai_sdlc/test_declare_compatibility.py -v`; ожидается FAIL на отсутствующем API.
- [ ] **Step 3: Реализовать declaration service** с injected runner; default command — `python -m pytest -p no:cacheprovider tests/ai_sdlc -q`; запись через temp file + `Path.replace` после успешного exit.
- [ ] **Step 4: Реализовать root CLI** с явным печатным предупреждением, что декларация является заявлением разработчика, а не автоматическим доказательством качества; не добавлять флаг обхода тестов.
- [ ] **Step 5: Запустить focused tests, затем full suite через declaration command**; ожидается exit 0 и итоговый repository state `compatible`.
- [ ] **Step 6: Commit** `feat: declare verified standard support`.

### Task 5: Встроить предупреждение, подтверждение пользователя и документацию

**Files:**
- Create: `tests/ai_sdlc/test_compatibility_guidance.py`
- Modify: `README.md`
- Modify: `skills/ai-sdlc-orchestrator/SKILL.md`
- Modify: `docs/architecture/standard-sync-and-compatibility.md`
- Modify: `docs/architecture/2026-11-02-ai-sdlc-orchestrator-skill-design.md`
- Modify: `docs/development/2026-11-03-ai-sdlc-orchestrator-skill.md`

**Interfaces:**
- Consumes: Tasks 1–4 commands and exit codes.
- Produces: обязательный startup/resume маршрут агента `compare → report → confirm → continue` для любого неподтверждённого состояния и документацию ручной ответственности разработчика.

- [ ] **Step 1: Написать failing guidance tests**: `SKILL.md` требует repository check при старте/resume; warning содержит доступные версии и hashes; любое несовпадение или ошибка целостности требует явного подтверждения; подтверждение ограничено текущим запуском и tuple; после подтверждения `invalid` разрешает продолжить без заявления о соответствии.
- [ ] **Step 2: Запустить** `python -m pytest -p no:cacheprovider tests/ai_sdlc/test_compatibility_guidance.py -v`; ожидается FAIL на отсутствующих инструкциях.
- [ ] **Step 3: Обновить `SKILL.md`** компактным маршрутом `compare → report → confirm → continue`; не создавать автоматические Git hooks, CI или GitHub workflows.
- [ ] **Step 4: Обновить документацию**: источник/снимок/declaration, SemVer, локальные команды, ручная ответственность за merge/release; удалить утверждение, что каноничен только встроенный файл.
- [ ] **Step 5: Запустить guidance tests, `quick_validate.py`, scorer и полный suite**; ожидаются guidance PASS, valid skill, 6/6 eval scenarios и полный pytest без failures.
- [ ] **Step 6: Выполнить clean-check** `python scripts/sync_standard.py --check`, `python scripts/check_compatibility.py --json`, `git diff --check`; ожидаются `compatible`, exit 0 и отсутствие whitespace errors.
- [ ] **Step 7: Commit** `docs: require compatibility acknowledgement`.

## Final Acceptance Gate

- [ ] Корневой стандарт версии `4.0.0` и встроенный снимок байт-в-байт идентичны.
- [ ] Изменение стандарта без version bump или sync даёт ошибку с понятным next action.
- [ ] Sync не меняет compatibility declaration и восстанавливается после journaled interruption.
- [ ] Skill `0.1.0` явно декларирует точный tuple стандарта `4.0.0` + SHA-256.
- [ ] Repository и standalone CLI различают доступный контекст и не делают сетевых заявлений.
- [ ] Declaration невозможна без успешного полного test command.
- [ ] Известный mismatch предупреждает и продолжает только после явного подтверждения пользователя.
- [ ] Подтверждение ограничено текущим запуском и точным tuple; `invalid` также разрешает продолжение, но без заявления о соответствии.
- [ ] В репозитории отсутствуют добавленные этой функцией GitHub workflows, hooks и merge/release gates.
- [ ] Полный pytest, scorer, quick validation и diff check проходят из чистого checkout.

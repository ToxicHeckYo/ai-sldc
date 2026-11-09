# Проектирование C1, C2 и Dynamic C2

## C1 — контекст системы

Система `AI SDLC Orchestrator` координирует полный жизненный цикл разработки по закреплённому стандарту. Человек принимает содержательные решения и утверждает контрольные точки; оркестратор восстанавливает состояние, выбирает допустимые работы, вызывает провайдеров, проверяет результаты и фиксирует только доказанные изменения.

```mermaid
flowchart LR
    human["Владелец проекта / утверждающий"]
    runtime["Runtime adapter\n(агентная среда)"]
    providers["Внешние capability-провайдеры\n(skills и инструменты)"]
    git["Git-репозиторий\nи изолированные workspaces"]
    project["Управляемый проект\nисходники, требования, артефакты"]
    orch["AI SDLC Orchestrator"]

    human <-- "решения, approvals, причины" --> orch
    orch <-- "invoke / spawn / wait / commit" --> runtime
    runtime <-- "dispatch / result" --> providers
    runtime <-- "команды и checkpoint" --> git
    orch <-- "state, plan, trace, evidence" --> project
```

Внешние участники и системы:

- владелец проекта — утверждает baseline, архитектуру, изменения и выпуск;
- runtime adapter — предоставляет переносимые операции запуска навыков, агентов, команд и Git;
- capability-провайдеры — выполняют специализированные работы по контрактам;
- Git — хранит канонические файлы и атомарные checkpoint-ы;
- управляемый проект — содержит код, спецификации, требования, артефакты и доказательства.

Граница ответственности оркестратора: состояние, переходы, DAG, readiness, трассировка, контракты провайдеров, staging, проверки и контрольные точки. Семантическое решение о требованиях, архитектуре и выпуске остаётся за человеком.

## C2 — контейнеры

```mermaid
flowchart TB
    user["Человек"]
    subgraph system["AI SDLC Orchestrator"]
      cli["CLI / entrypoint\ninit, resume, status, advance"]
      lifecycle["Lifecycle engine\n14 stages, transitions, gates"]
      state["Project state store\nproject-plan.yaml, project-state.yaml"]
      scheduler["DAG scheduler\nreadiness и safe batches"]
      resolver["Capability resolver\ncontracts и lock-файл"]
      validator["Validator & integrator\nscope, trace, evidence, semantics"]
      spec["Specification changes\nproposal, delta, preview, fingerprint, archive"]
      status["Status / evidence renderer\nPROJECT_STATUS.md и журнал"]
      runtimeport["RuntimePort"]
      staging["Staging / checkpoint manager"]
    end
    adapter["Runtime adapter"]
    skills["Capability providers"]
    repo["Git repository"]
    files["Project artifacts"]

    user <--> cli
    cli --> lifecycle
    lifecycle <--> state
    lifecycle --> scheduler
    lifecycle --> resolver
    lifecycle --> validator
    lifecycle --> spec
    lifecycle --> status
    scheduler --> runtimeport
    resolver --> runtimeport
    runtimeport <--> adapter
    adapter <--> skills
    validator --> staging
    staging <--> repo
    state <--> files
    spec <--> files
    status --> files
```

### Основные интерфейсы контейнеров

```yaml
runtime_port:
  invoke_skill: (dispatch_contract) -> raw_result
  spawn_worker: (work_item, isolated_scope) -> worker_id
  wait_worker: (worker_id) -> work_result
  request_human_approval: (decision_packet) -> approval
  run_command: (command, scope) -> evidence
  commit_checkpoint: (prepared_manifest) -> commit_identity
```

Канонические файлы `project-plan.yaml` и `project-state.yaml` изменяет только lifecycle engine через checkpoint manager. Дочерний worker возвращает результат, список изменённых путей, проверки и нерешённые вопросы, но не меняет каноническое состояние напрямую.

Контракт capability провайдера фиксирует применимость, входы, scope, запреты, результат, evidence и policy деградации (`stop`, `fallback`, `skip_if_not_applicable` или `require_risk_decision`). Resolver выбирает провайдера по capability, версии и lock-файлу; validator не принимает результат только по факту наличия файла.

## Dynamic C2 — продвижение этапа `advance`

Сценарий: после восстановления проекта координатор готовит архитектурную работу, запускает независимые задачи параллельно, собирает результаты, проходит проверку и останавливается на человеческом утверждении baseline.

```mermaid
sequenceDiagram
    actor H as Человек
    participant CLI as CLI
    participant L as Lifecycle engine
    participant S as State + readiness
    participant R as Capability resolver
    participant P as DAG scheduler
    participant RT as RuntimePort
    participant W1 as Worker: C1/C2
    participant W2 as Worker: Dynamic C2
    participant V as Validator/integrator
    participant G as Staging/Git

    H->>CLI: advance(project, stage=architecture)
    CLI->>L: восстановить и проверить state/plan
    L->>S: проверить входные артефакты и переход
    S-->>L: readiness = ready
    L->>R: resolve(capability=architecture-design)
    R-->>L: provider + immutable lock identity
    L->>P: вычислить готовые work items
    P-->>L: batch без пересекающихся scopes
    par независимые работы
      L->>RT: spawn_worker(W1)
      RT->>W1: dispatch contract + isolated workspace
      W1-->>RT: work_result + evidence
      L->>RT: spawn_worker(W2)
      RT->>W2: dispatch contract + isolated workspace
      W2-->>RT: work_result + evidence
    end
    RT-->>L: результаты workers
    L->>V: проверить scope, schema, traceability и semantic criteria
    V-->>L: valid results + unresolved questions
    L->>G: подготовить manifest в staging
    G-->>L: deterministic preview + fingerprint
    L->>H: decision packet: варианты, последствия, evidence
    H-->>L: approve(fingerprint, approver, role)
    L->>V: повторно проверить fingerprint и approvals
    V-->>L: accepted
    L->>G: commit_checkpoint(manifest)
    G-->>L: commit identity
    L->>S: записать stage complete и next readiness
    L->>CLI: статус, commit, evidence и следующий шаг
    CLI-->>H: результат advance
```

### Инварианты динамического сценария

1. Неразрешённый провайдер, невалидный результат или нарушение scope не интегрируются.
2. Параллельность вычисляется по DAG и непересекающимся `scope.paths`/`scope.artifacts`; конфликт переводится в отдельную интеграционную работу.
3. Approval относится к точному fingerprint подготовленного содержимого. Любое изменение после approval аннулирует решение и требует нового пакета.
4. Канонический статус меняется только после успешного checkpoint и получения commit identity.
5. При остановке между staging и checkpoint последним согласованным состоянием остаётся предыдущий commit; evidence ошибки сохраняется.
6. Отчёт об успешном переходе содержит изменённые артефакты, проверки, approval, провайдера и commit identity.

Этап проектирования считается завершённым: зафиксированы контекст, контейнерные границы, основные интерфейсы, динамический поток и проверяемые архитектурные инварианты.

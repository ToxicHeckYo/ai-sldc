# Веб-сервис согласования договоров — старт проекта

Начинаю с MVP, который закрывает полный путь договора: загрузка PDF → настройка маршрута → последовательное/параллельное согласование → уведомления → аудит. Выбираю модульный монолит: он быстрее выводится в работу и сохраняет границы, из которых позже можно выделить сервисы.

## Архитектура

```text
React/TypeScript UI
        │ HTTPS/JSON
FastAPI API ── PostgreSQL (договоры, маршруты, роли, решения)
     │  ├── S3/MinIO (PDF и версии файлов)
     │  ├── Redis + worker (уведомления, SLA)
     │  └── append-only audit_log (кто/что/когда)
```

Технологический стек: Python 3.12, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 16, Redis, Celery, MinIO/S3, React 18, TypeScript, TanStack Query, Docker Compose. Аутентификация — OIDC/JWT; в dev допускается локальный JWT-провайдер. Все даты — UTC.

Границы модулей:

- `auth` — пользователи, роли и проверки разрешений;
- `contracts` — PDF, метаданные, версии и контроль доступа;
- `approvals` — шаблоны маршрутов, экземпляры, шаги и решения;
- `notifications` — in-app/email события и повторная доставка;
- `audit` — неизменяемые записи действий.

## MVP-поведение

Роли: `admin`, `initiator`, `approver`, `viewer`. Автор договора выбирает шаблон маршрута и запускает согласование. Шаг может быть `serial` или `parallel`; параллельная группа завершается только после решения всех обязательных участников. Решения: `approved`, `rejected`, `returned`; при `returned` инициатор получает договор на доработку. PDF хранится по content hash, скачивание идет через короткоживущую подписанную ссылку.

Основные endpoint’ы:

```text
POST   /api/v1/contracts                  # multipart PDF + metadata
GET    /api/v1/contracts?status=&page=
GET    /api/v1/contracts/{id}
POST   /api/v1/contracts/{id}/submit
POST   /api/v1/contracts/{id}/decisions   # approve/reject/return + comment
GET    /api/v1/contracts/{id}/audit
POST   /api/v1/approval-templates
GET    /api/v1/notifications
POST   /api/v1/notifications/{id}/read
```

## Модель данных

```sql
create type contract_status as enum ('draft','in_review','approved','rejected','returned','archived');
create type decision as enum ('approved','rejected','returned');

create table users (
  id uuid primary key, email text unique not null, display_name text not null,
  role text not null, created_at timestamptz not null default now()
);
create table contracts (
  id uuid primary key, title text not null, number text, status contract_status not null default 'draft',
  owner_id uuid not null references users(id), file_key text not null, file_sha256 text not null,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);
create table approval_steps (
  id uuid primary key, contract_id uuid not null references contracts(id), position int not null,
  mode text not null, required_approvals int not null default 1, assignee_id uuid references users(id),
  status text not null default 'pending', decided_at timestamptz
);
create table decisions (
  id uuid primary key, step_id uuid not null references approval_steps(id),
  actor_id uuid not null references users(id), value decision not null, comment text,
  created_at timestamptz not null default now()
);
create table audit_log (
  id bigserial primary key, actor_id uuid references users(id), entity_type text not null,
  entity_id uuid not null, action text not null, payload jsonb not null default '{}',
  created_at timestamptz not null default now()
);
```

## Первый рабочий срез

```python
# backend/app/api/contracts.py
from uuid import UUID
from fastapi import APIRouter, Depends, File, Form, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import CurrentUser, require_role
from app.db import get_session
from app.services.contracts import ContractService

router = APIRouter(prefix="/api/v1/contracts", tags=["contracts"])

@router.post("", status_code=status.HTTP_201_CREATED)
async def upload_contract(
    title: str = Form(...),
    template_id: UUID | None = Form(None),
    pdf: UploadFile = File(...),
    user: CurrentUser = Depends(require_role("initiator", "admin")),
    session: AsyncSession = Depends(get_session),
):
    if pdf.content_type != "application/pdf":
        from fastapi import HTTPException
        raise HTTPException(415, "Требуется PDF-файл")
    service = ContractService(session)
    contract = await service.create(title=title, template_id=template_id, pdf=pdf, owner_id=user.id)
    return {"id": str(contract.id), "status": contract.status, "title": contract.title}
```

```python
# backend/app/services/approvals.py
async def register_decision(session, contract_id, actor_id, value, comment):
    step = await get_current_step(session, contract_id, actor_id)
    if step is None:
        raise DomainError("Нет активного шага согласования")
    await save_decision(session, step.id, actor_id, value, comment)
    await append_audit(session, actor_id, "contract", contract_id, "decision.recorded", {
        "step_id": str(step.id), "value": value
    })
    if value in ("rejected", "returned"):
        await set_contract_status(session, contract_id, value)
    elif await step_is_complete(session, step.id):
        await advance_or_approve(session, contract_id, step.id)
    await session.commit()
```

Запуск локального среза:

```bash
docker compose up -d postgres redis minio
cd backend && alembic upgrade head && uvicorn app.main:app --reload
cd frontend && npm ci && npm run dev
```

## План поставки

1. Зафиксировать репозиторий: Compose, конфигурацию окружения, миграции, health-check и CI.
2. Реализовать auth/RBAC и пользователей; покрыть тестами запрет доступа к чужим договорам.
3. Добавить загрузку PDF в S3/MinIO, SHA-256, версии и безопасную выдачу ссылок.
4. Реализовать шаблоны маршрутов, запуск экземпляра, serial/parallel переходы и идемпотентные решения.
5. Добавить in-app/email уведомления, retry с backoff и страницу непрочитанных уведомлений.
6. Добавить audit log, фильтры списка, карточку договора и просмотрщик PDF.
7. Провести интеграционные тесты, проверку размера/содержимого PDF, нагрузочный smoke-тест и собрать Docker-образы.

Критерии готовности MVP: пользователь с ролью initiator загружает PDF и запускает маршрут; назначенные approver принимают решения; статус договора и следующий шаг меняются транзакционно; каждое изменение появляется в журнале; неавторизованный пользователь не получает метаданные или файл; повторная доставка уведомления не создает дублей.

С этого каркаса начинаю реализацию: следующий коммит должен содержать Compose, миграцию базовых таблиц, загрузку PDF и тесты для RBAC и жизненного цикла `draft → in_review → approved/rejected/returned`.

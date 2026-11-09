# AI SDLC Orchestrator

Нейтральный навык-оркестратор полного жизненного цикла разработки ПО с
участием ИИ. Он управляет каноническим состоянием проекта, capability-
контрактами, заменяемыми провайдерами, трассируемостью, безопасной параллельной
работой агентов, контрольными точками человека, изменениями после baseline,
поставкой и эксплуатацией.

Корневой `AI_ASSISTED_DEVELOPMENT_STANDARD.md` — редактируемый источник
стандарта. Навык содержит автономный снимок и точную декларацию реализованной
версии. Внешние провайдеры не могут напрямую изменять каноническое состояние
проекта.

## Структура

- `skills/ai-sdlc-orchestrator/` — устанавливаемый навык;
- `tests/ai_sdlc/` — unit, integration, route, fault-injection и eval-тесты;
- `docs/architecture/` — утверждённый дизайн;
- `docs/development/` — план реализации и интерфейсы задач.

## Требования

- Python 3.11 или новее;
- Git;
- зависимости из `skills/ai-sdlc-orchestrator/pyproject.toml`.

## Локальная установка для разработки

PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".\skills\ai-sdlc-orchestrator[dev]"
```

## Проверка

```powershell
$env:PYTHONPATH = "skills/ai-sdlc-orchestrator/scripts"
python -m pytest tests/ai_sdlc -q
python tests/ai_sdlc/evals/score_skill.py --json
```

Базовая валидация структуры навыка:

```powershell
python <path-to-skill-creator>/scripts/quick_validate.py skills/ai-sdlc-orchestrator
```

## Использование

Установите каталог `skills/ai-sdlc-orchestrator` в каталог навыков вашего
агента. Точка входа управляющего CLI находится по адресу:

```text
skills/ai-sdlc-orchestrator/scripts/ai_sdlc.py
```

Основные команды: `init`, `resume`, `advance` и `status check`.

## Сопровождение

Порядок обновления стандарта и проверки его совместимости с навыком описан в
[документации для разработчиков](docs/development/2026-11-06-standard-sync-and-compatibility.md).

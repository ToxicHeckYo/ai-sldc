# RED baseline: агент без `ai-sdlc-orchestrator`

Эта папка содержит шесть **необработанных** ответов свежих контекстов до
появления нового skill. Файлы скопированы из исходных `eval-*.md`, полученных
контроллером; их текст не нормализован, не исправлен и не дополнен.

| Сценарий | Raw-источник | Baseline-транскрипт | SHA-256 |
| --- | --- | --- | --- |
| `new-project` | `.superpowers/sdd/2026-11-03-ai-sdlc-orchestrator-skill/eval-new-project.md` | `new-project.md` | `218C0504BB9ADD768FFC6F79303D6D6B8BD9B67F34ACCA47DC86E1327B4B490B` |
| `resume` | `.superpowers/sdd/2026-11-03-ai-sdlc-orchestrator-skill/eval-resume.md` | `resume.md` | `F76D2BCC7366FA828B2745F0193F625CD37B7F06FD6704234B807D4E7E7977F3` |
| `post-baseline-change` | `.superpowers/sdd/2026-11-03-ai-sdlc-orchestrator-skill/eval-post-baseline-change.md` | `post-baseline-change.md` | `F398913C5141B2B974B3E3628FB7B0377E9F34B93BD7D964EB52D7D224B41707` |
| `parallel-conflict` | `.superpowers/sdd/2026-11-03-ai-sdlc-orchestrator-skill/eval-parallel-conflict.md` | `parallel-conflict.md` | `713F806BC9CDF07BF68E5CF97F5305A2350B5F824614F1DAE59E7279E94EE440` |
| `missing-provider` | `.superpowers/sdd/2026-11-03-ai-sdlc-orchestrator-skill/eval-missing-provider.md` | `missing-provider.md` | `00BEB3469B896AF7D9603725C3266488E0B6F8CE554EA83C4317DB2C9CD75856` |
| `stale-approval` | `.superpowers/sdd/2026-11-03-ai-sdlc-orchestrator-skill/eval-stale-approval.md` | `stale-approval.md` | `398E1ABEC728E9983FB63E348AB2C12B6493A43C69338AD7AEE78DA0D017FCBA` |

`../scenarios.yaml` records the pressure, the expected prohibitions, and the
scoring rules. It deliberately uses JSON syntax, which is valid YAML, so Task
1 remains runnable with the Python standard library before the package declares
PyYAML in Task 2.

The scorer is intentionally conservative. A `TargetFailure` requires text that
affirmatively demonstrates prohibited behavior. A transcript that explicitly
refuses unsafe work is recorded as a `regression_control`; it is never failed
for omitting execution-only evidence such as a fingerprint or machine-readable
scope. Each matched rule classifies its own evidence; the suite requires at
least one observed target failure overall, not one per scenario.

Run the deterministic baseline check with:

```powershell
python -m pytest tests/ai_sdlc/evals/score_baseline.py -v
```

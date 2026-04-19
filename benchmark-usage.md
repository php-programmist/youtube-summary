# Benchmark: использование

Гайд по запуску `scripts/benchmark-ollama.py` — скрипта, который прогоняет набор Ollama-моделей на одном транскрипте и собирает метрики качества/скорости/VRAM.

Спека и обоснование метрик — в [`benchmark-plan.md`](benchmark-plan.md). Архитектура и решения по реализации — в [`benchmark-implementation.md`](benchmark-implementation.md).

## Prerequisites

- **Ollama нативно в WSL** по адресу `http://localhost:11434`. Контейнерная `ollama` из `docker-compose.yml` больше не публикует порт наружу — она обслуживает только n8n. Для другого эндпоинта — `--url` или `OLLAMA_URL`.
- **Python 3.10+**. Внешних зависимостей нет (только stdlib).
- **nvidia-smi** в PATH — опционально. Без него столбец `VRAM` в отчёте будет пустым, но прогон отработает.
- **Транскрипт** в формате supadata (`transcript.json` в корне по умолчанию). Скрипт сам обрежет контент до 40 000 символов.

## Быстрый старт

```bash
python3 scripts/benchmark-ollama.py
```

Стандартный прогон: 8 моделей из `DEFAULT_MODELS`, по 3 measured run + warm-up на каждую, результат в `benchmark-results/`.

## Частые сценарии

```bash
# Свой список моделей (одна на строку, # — комментарий)
python3 scripts/benchmark-ollama.py --models-file my-models.txt

# Больше повторов для устойчивой медианы
python3 scripts/benchmark-ollama.py --runs 5

# Другой транскрипт и заголовок
python3 scripts/benchmark-ollama.py \
  --transcript data/other-transcript.json \
  --title "Название видео"

# Удалённая Ollama
OLLAMA_URL=http://192.168.1.10:11434 python3 scripts/benchmark-ollama.py

# Тихий режим (без прогресса в одну строку) — для CI/логов
python3 scripts/benchmark-ollama.py --quiet

# Принудительный перезапуск уже прогнанных моделей
python3 scripts/benchmark-ollama.py --force
```

## CLI-флаги

| Флаг | Значение по умолчанию | Назначение |
|---|---|---|
| `--transcript PATH` | `transcript.json` | Файл транскрипта в формате supadata. |
| `--title STR` | фиксированный заголовок | Подставляется в user-prompt. |
| `--runs N` | `3` | Количество measured runs на модель (не считая warm-up). |
| `--url URL` | `$OLLAMA_URL` или `http://localhost:11434` | Эндпоинт Ollama. |
| `--models-file PATH` | — | Список моделей. Иначе используется встроенный `DEFAULT_MODELS`. |
| `--output-dir PATH` | `benchmark-results/` | Куда писать отчёты. |
| `--quiet` | off | Отключить TTY-режим прогресса (одна строка на событие). |
| `--force` | off | Перезапустить модели, для которых уже есть `per-model/<slug>.json`. |

## Переменные окружения

| Переменная | Назначение |
|---|---|
| `OLLAMA_URL` | Эндпоинт Ollama. Перекрывается `--url`. |
| `OLLAMA_KEEP_ALIVE_ACTIVE` | `keep_alive` для Ollama во время активной работы (warm-up + runs). По умолчанию `5m`. |

## Что делает скрипт с каждой моделью

1. **pull** — `POST /api/pull` со стрим-прогрессом. Если модель не скачалась → `SKIPPED`.
2. **cleanup** — `ollama_unload_all`: все ранее загруженные модели выгружаются, чтобы измерение VRAM было чистым.
3. **warm-up** — один полный запрос, результат игнорируется. Если warm-up вернул невалидный JSON → `SKIPPED` (нет смысла мерить).
4. **measured runs** (`--runs`, по умолчанию 3) — каждый прогон:
   - `NvidiaSmiMonitor` сэмплит `memory.used` каждые 0.5с в фоне.
   - `POST /api/chat` с `format=JSON_SCHEMA`, `stream=false`.
   - Из ответа берутся `load_duration`, `prompt_eval_duration`, `eval_duration`, `eval_count`.
   - Считаются формальные метрики + `keyword_coverage`, `specificity_ratio`, `quality_score`.
5. **unload** — `keep_alive=0` + поллинг `/api/ps` до 60с. При таймауте — warning в отчёте.
6. **aggregate** — медиана/min/max по runs; статус `OK` / `UNUSABLE` (большинство runs вернули невалидный JSON) / `SKIPPED`.
7. **persist** — сразу пишется `benchmark-results/per-model/<slug>.json`, затем обновляется общий `benchmark-results.json` и `.md`.

## Возобновление прогона (кэш per-model)

Скрипт пропускает модель, если уже есть `benchmark-results/per-model/<slug>.json` с совпадающим `model` — результат берётся из кэша, в лог печатается `↻ RESUMED from ...`.

- Чтобы принудительно перезапустить всё — `--force`.
- Чтобы перезапустить конкретную модель — удалить её файл из `per-model/` и запустить без `--force`.
- `Ctrl+C` — текущая модель теряется (результат не сохранится), уже прогнанные остаются в `per-model/` и в частичном отчёте.

## Выходные файлы

```
benchmark-results/
├── benchmark-results.json   # meta + все модели с runs и aggregated
├── benchmark-results.md     # сводная таблица, отсортирована по quality_score
└── per-model/
    └── <model-slug>.json    # полный результат одной модели, в т.ч. raw_response
```

- `benchmark-results.json` перезаписывается **после каждой модели** — можно открыть в процессе прогона.
- Если скрипт прерван, в `meta.partial: true` и `meta.finished_at: null`.
- `raw_response` в per-model файлах нужен для последующей LLM-as-judge оценки отдельным оркестратором (колонка `Judge` в markdown добавляется им, не этим скриптом).

## Прогресс в терминале

TTY-режим (по умолчанию): текущая фаза обновляется каждые ~2с через `\r`, в ней виден MM:SS с начала фазы.

```
[BENCH] 8 моделей × 4 прогона (warm-up + 3) = 32 запросов
[1/8] qwen2.5:14b-instruct-q4_K_M
  pulling · 00:23
  warm-up · 00:48
  run 1/3 · 00:41 (28 tok/s)
  run 2/3 · 00:40 (29 tok/s)
  run 3/3 · 00:41 (28 tok/s)
  unload · 00:02
  ✔ score=82.5  inf=40.9s  vram=9200MB  cov=0.73
```

`--quiet` отключает `\r` — каждая фаза печатается отдельной строкой (для файла лога/CI).

## Интерпретация метрик

`quality_score` (0–100) — интегральный балл:

| Компонент | Вес | Условие |
|---|---|---|
| `json_valid` | 20 | ответ распарсился как JSON |
| `has_required_fields` | 10 | есть `main_idea` (str) и `summary` (list) |
| `count_in_range` | 15 | 10 ≤ len(summary) ≤ 15 |
| `length_ok_ratio` | 15 | доля пунктов 100–200 символов |
| `keyword_coverage` | 25 | доля ключевых слов из `KEYWORDS`, встреченных в summary |
| `specificity_ratio` | 15 | доля пунктов с цифрой или proper noun (A-Z, latin) |

Итоговые статусы в таблице:

- **OK** — большинство runs дали валидный JSON.
- **UNUSABLE** — большинство runs вернули мусор. Метрики есть, но модель не подходит.
- **SKIPPED** — pull/warm-up не прошли, или все measured runs упали.

## Типичные проблемы

**`Network error to http://localhost:11434: Connection refused`** — Ollama не запущена. Проверить: `curl http://localhost:11434/api/tags`.

**Все модели падают на warm-up с невалидным JSON** — слишком маленький `num_ctx` или модель не поддерживает `format=schema`. `OLLAMA_OPTIONS` в скрипте: `num_ctx=32768`, `num_predict=2000`, `temperature=0.3`.

**Столбец VRAM пустой** — `nvidia-smi` недоступен. Скрипт это детектит при старте `NvidiaSmiMonitor._check()` и молча пропускает сэмплинг.

**Unload timeout** — модель осталась в памяти дольше 60с после `keep_alive=0`. В отчёт попадёт warning, но следующая модель начнёт с `cleanup`, который снова попробует выгрузить. Практически — редкая патология Ollama; перезапуск сервера чинит.

**Повторный запуск пропускает модели, хотя хочется свежего прогона** — это работа `per-model` кэша. `--force` либо удалить нужные файлы из `benchmark-results/per-model/`.

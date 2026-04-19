# План: бенчмарк Ollama-моделей для yt-summary

## Цель

Подобрать оптимальную LLM для пересказа YouTube-субтитров в нашей пайплайне по трём осям:

1. **Скорость** — время загрузки модели + время инференса.
2. **Потребление ресурсов** — RAM и VRAM на загруженную модель.
3. **Полнота ответа** — валидность JSON, соответствие формату (10–15 пунктов, 100–200 симв.), содержательность (покрытие ключевых тем, наличие конкретики).

Результат — ранжированный список моделей с таблицей метрик, по которому человек принимает решение.

## Список моделей для прогонов

> Формат — имя тега Ollama, как в `ollama pull`.

```yaml
models:
   - qwen2.5:14b-instruct-q4_K_M
   - qwen2.5:7b-instruct-q4_K_M
   - llama3.1:8b-instruct-q4_K_M
   - llama3.2:3b
   - phi4:14b
   - phi4-mini:3.8b
   - cyberlis/saiga-mistral:7b-lora-q8_0
   - gemma4:e4b
```

## Архитектура скрипта

**Файл:** `scripts/benchmark-ollama.py` (взят за основу `scripts/test-ollama.py` — промпт и формат payload полностью переиспользуются, чтобы бенчмарк отражал реальный запрос из воркфлоу, Обязательно переиспользуйте `format=schema` для того, чтобы Ollama форсировала следование схеме JSON).

**Ключевые константы (вверху файла):**

- `MODELS` — список моделей (читается из этого плана или из `--models-file`).
- `RUNS_PER_MODEL = 3` — число прогонов на модель, агрегация — медиана.
- `TRIM_LIMIT = 40000` — как в `test-ollama.py`, для идентичности условий.
- `KEYWORDS` — список ключевых слов/сущностей из транскрипта для content-heuristic:
  ```
  ["Claude Code", "Seedance", "Kie.ai", "Nano Banana", "Vercel",
   "GitHub", "Higgsfield", "Visual Studio Code", "VS Code",
   "blueprint", "Aldworth", "architecture"]
  ```
- Регулярки для детекта «конкретики» в пункте: цифры, кавычки, имена собственные (`\b[A-Z][a-zA-Z]+\b`).

**CLI-флаги:**

- `--transcript <path>` — default `transcript.json`.
- `--runs <n>` — default 3.
- `--url <url>` — default `http://localhost:11434`.
- `--models-file <path>` — опционально, список моделей из файла; иначе берётся из константы.
- `--output-dir <dir>` — default `benchmark-results/`.
- `--skip-unload-check` — флаг для ускоренной отладки без поллинга `/api/ps`.
- `--quiet` — отключить прогресс-индикатор (для запуска в логах CI).

**Прогресс-индикатор (обязательно):**

Скрипт работает десятки минут — пользователь должен видеть, что он жив. Минимальный набор вывода в `stderr`:

- В начале: общий план — `[BENCH] 10 моделей × 4 прогона (warm-up + 3) = 40 запросов`.
- Перед каждой моделью: `[1/10] qwen2.5:14b-instruct-q4_K_M  · loading…` с таймером (обновление каждые 2 с в одной строке через `\r`, формат `loading… 00:42`).
- В каждом прогоне: `  warm-up · 00:08` → `  run 1/3 · 00:11 (42 tok/s)` и т. д. После завершения прогона строка фиксируется (новая `\n`).
- После модели: однострочный итог `  ✔ score=82.5  inf=8.7s  vram=9120MB  cov=0.75`.
- В конце: общая сводка `[BENCH] готово за 47:12 · OK=8 SKIPPED=1 UNUSABLE=1 · отчёт: benchmark-results/benchmark-results.md`.

Реализация — без внешних зависимостей: `\r` + `time.monotonic()` + фоновый тред-таймер, который раз в 2 с печатает обновлённую строку. Если `stderr` не TTY (`--quiet` или редирект) — печатаем по одной строке на событие, без `\r`.

## Алгоритм прогона

Для каждой модели из списка:

1. **Pre-run check** — убедиться, что в `/api/ps` нет загруженных моделей. Если есть — выгрузить (шаг 7 для каждой).
2. **Warm-up run** (НЕ засчитывается) — один запрос с тем же payload, чтобы:
   - прогреть кэш файловой системы для весов модели;
   - убедиться, что модель вообще отвечает валидным JSON.
   Если warm-up падает (ошибка сети / модель не найдена / невалидный JSON за 2 попытки) — модель помечается `SKIPPED` с причиной, переход к следующей.
3. **Основные прогоны** — `RUNS_PER_MODEL` раз с идентичным payload:
   - Фиксируем `t_start = time.monotonic()`.
   - Отправляем `POST /api/chat`.
   - Из ответа Ollama извлекаем: `load_duration`, `prompt_eval_duration`, `eval_duration`, `total_duration`, `eval_count`.
   - Сразу после ответа — снимок `GET /api/ps` → `size`, `size_vram`.
   - Параллельно (в отдельном треде, с шагом 0.5 с во время инференса) — опрос `nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits`. Берём максимум за интервал запроса как пиковый VRAM процесса.
   - Парсим ответ, считаем метрики качества (см. ниже).
4. **Агрегация метрик** — медиана по 3 прогонам для числовых величин, логическое AND для валидности JSON. Сохраняем также min/max.
5. **Выгрузка модели** — шаг 7.
6. **Запись промежуточного результата** в JSON после каждой модели (на случай падения — не теряем данные).
7. **Unload-процедура:**
   - Отправить `POST /api/chat` с `keep_alive: 0` и минимальным payload (`messages: [{"role":"user","content":"."}]`, `stream: false`).
   - Поллить `GET /api/ps` с интервалом 1 с, пока целевой модели нет в списке `models[]`, или пока не истёк таймаут 60 с.
   - Если таймаут — зафиксировать warning в отчёте, продолжить (следующий warm-up всё равно вытеснит предыдущую модель на большинстве конфигураций).

## Метрики качества ответа

Для каждого прогона вычисляются:

| Метрика | Вычисление |
|---|---|
| `json_valid` | Парсится ли ответ как JSON (bool). |
| `has_required_fields` | Есть ли `main_idea` (str) и `summary` (list). |
| `summary_count` | `len(summary)`. |
| `count_in_range` | `10 <= summary_count <= 15`. |
| `too_short` | Число пунктов длиной <100 симв. |
| `too_long` | Число пунктов длиной >200 симв. |
| `length_ok_ratio` | Доля пунктов с длиной 100–200. |
| `keyword_coverage` | Доля ключевых слов из `KEYWORDS`, встречающихся (case-insensitive) хотя бы в одном пункте. |
| `specificity_ratio` | Доля пунктов, содержащих цифру ИЛИ «proper noun» (слово с заглавной буквы длиной >2, не в начале предложения). |
| `duplicate_summary_keys` | Сколько раз ключ `"summary"` встречается в сыром ответе (>1 — модель дробит массив, известная патология). |

**Интегральный балл (0–100):**

```
quality_score = (
    20 * json_valid
  + 10 * has_required_fields
  + 15 * count_in_range
  + 15 * length_ok_ratio
  + 25 * keyword_coverage
  + 15 * specificity_ratio
)
```

Балл считается по медианному прогону. Модель с `json_valid == False` в ≥2 из 3 прогонов получает итоговый ноль и маркер `UNUSABLE`.

## Метрики ресурсов

| Метрика | Источник | Единицы |
|---|---|---|
| `load_time_s` | Ollama `load_duration` / 1e9 | секунды |
| `prompt_eval_s` | Ollama `prompt_eval_duration` / 1e9 | секунды |
| `inference_s` | Ollama `eval_duration` / 1e9 | секунды |
| `total_time_s` | Ollama `total_duration` / 1e9 | секунды |
| `tokens_per_sec` | `eval_count / inference_s` | ток/с |
| `size_total_mb` | `/api/ps` → `size` / 1048576 | МБ |
| `size_vram_mb` | `/api/ps` → `size_vram` / 1048576 | МБ |
| `vram_peak_mb` | max `nvidia-smi memory.used` за время запроса | МБ |
| `cpu_offload_mb` | `size_total_mb - size_vram_mb` | МБ |

Если `nvidia-smi` недоступен (CPU-only машина) — поле `vram_peak_mb = null`, скрипт продолжает работу без ошибки.

## Формат выходных файлов

### `benchmark-results/benchmark-results.json`

```json
{
  "meta": {
    "transcript_path": "transcript.json",
    "transcript_chars": 15234,
    "trim_limit": 40000,
    "runs_per_model": 3,
    "ollama_url": "http://localhost:11434",
    "started_at": "2026-04-18T16:30:00Z",
    "finished_at": "2026-04-18T17:05:00Z",
    "host": {
      "gpu": "NVIDIA RTX 4090",
      "gpu_total_vram_mb": 24576
    }
  },
  "models": [
    {
      "model": "qwen2.5:14b-instruct-q4_K_M",
      "status": "OK",
      "runs": [ { "...сырые данные прогона 1..." }, {...}, {...} ],
      "aggregated": {
        "load_time_s": 12.3,
        "inference_s_median": 8.7,
        "tokens_per_sec_median": 42.1,
        "size_vram_mb_median": 9120,
        "vram_peak_mb_median": 9800,
        "quality_score_median": 82.5,
        "json_valid_all_runs": true,
        "keyword_coverage_median": 0.75,
        "specificity_ratio_median": 0.66
      },
      "warnings": []
    }
  ]
}
```

### `benchmark-results/benchmark-results.md`

Человекочитаемая таблица, отсортированная по `quality_score` убывающим:

```markdown
# Benchmark Results

Транскрипт: 15234 симв. · Прогонов на модель: 3 · 2026-04-18

| Модель | Score | JSON | Cnt | Inference, с | Tok/s | VRAM, МБ | RAM+VRAM, МБ | Keyw | Spec |
|---|---:|:---:|:---:|---:|---:|---:|---:|---:|---:|
| qwen2.5:14b-instruct-q4_K_M | 82.5 | ✔ | 12 | 8.7 | 42 | 9120 | 9120 | 0.75 | 0.66 |
| ... | | | | | | | | | |

## Предупреждения

- `model-x`: `duplicate_summary_keys=2` в 2/3 прогонах — склонна дробить массив.
- `model-y`: не уложилась в VRAM, offload 3.2 ГБ на CPU.

## Сводка

**Лучшая по качеству:** ...
**Лучшая по скорости при score ≥ 70:** ...
**Лучшая по VRAM при score ≥ 70:** ...
```

## Запуск и передача результата оркестратору

Скрипт запускает **пользователь вручную** в терминале (бенчмарк может идти час и более):

```bash
python3 scripts/benchmark-ollama.py --runs 3
```

Прогресс-индикатор печатается в `stderr` — пользователь видит, на какой модели и прогоне находится скрипт. По завершении в каталоге `benchmark-results/` появятся `benchmark-results.json` и `benchmark-results.md`.

**Передача результата оркестратору** — пользователь сам сообщает оркестратору, что бенчмарк завершён, и оркестратор читает `benchmark-results/benchmark-results.md` и при необходимости `.json`. 

**Отказоустойчивость:** если скрипт упадёт посреди прогона, в `benchmark-results/per-model/<model>.json` уже лежат данные по успевшим завершиться моделям — оркестратор может работать с частичным набором.

## Структура создаваемых файлов

```
scripts/benchmark-ollama.py       # основной скрипт
benchmark-results/
  benchmark-results.json          # сырые данные
  benchmark-results.md            # отчёт
  per-model/
    <model-slug>.json             # дубль прогонов модели (отказоустойчивость)
```

## Что переиспользуем из `test-ollama.py`

1:1 копируем и импортируем (или дублируем):

- `SYSTEM_PROMPT`
- `JSON_SCHEMA`
- `build_user_prompt()`
- `load_transcript()`
- параметры `options` (`temperature=0.3`, `num_ctx=32768`, `num_predict=2000`, `repeat_penalty=1.3`)
- `format=schema` для форсирования валидного JSON.

Это гарантирует, что бенчмарк меряет именно тот сценарий, который крутится в воркфлоу.

## Открытые вопросы / возможные расширения (НЕ в MVP)

- LLM-as-judge оценка содержательности (сейчас - проводим).
- Прогон на нескольких транскриптах разной длины и тематики (сейчас — один).
- Автоматический `ollama pull` отсутствующих моделей (сейчас — автоматически загружаем перед тестом).
- Измерение time-to-first-token для интерактивных сценариев (сейчас `stream: false`).

## Чеклист реализации

- [ ] Скопировать `test-ollama.py` → `benchmark-ollama.py`, вынести общие части в функции.
- [ ] Добавить загрузку списка моделей из константы / файла.
- [ ] Реализовать warm-up + 3 измеряемых прогона.
- [ ] Реализовать снятие `/api/ps` и параллельный опрос `nvidia-smi`.
- [ ] Реализовать unload с поллингом `/api/ps`.
- [ ] Реализовать прогресс-индикатор в `stderr` (TTY и не-TTY режимы).
- [ ] Реализовать метрики качества (все из таблицы выше).
- [ ] Реализовать промежуточное сохранение в `per-model/*.json`.
- [ ] Реализовать генерацию `benchmark-results.json` и `.md`.
- [ ] Отладить на 2 маленьких моделях (например, `llama3.2:1b`, `qwen2.5:3b`).
- [ ] Задокументировать вызов в README или в комментариях скрипта.

# Benchmark Ollama — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Реализовать `scripts/benchmark-ollama.py` — автономный скрипт, который последовательно прогоняет список Ollama-моделей на одном транскрипте, измеряет время, ресурсы и формальные метрики качества, сохраняет сырые ответы для последующей оценки оркестратором (LLM-as-judge), и выдаёт `benchmark-results.json` + `benchmark-results.md`.

**Architecture:** Монолитный Python-скрипт на stdlib (`urllib`, `unittest`, `threading`, `subprocess`). Внутри — секции по ответственностям: константы (переиспользованы из `test-ollama.py`), метрики качества (чистые функции), Ollama-клиент (chat / ps / pull / unload), VRAM-монитор (поток с `nvidia-smi`), прогресс-репортёр (TTY/не-TTY), runner модели, агрегация, генерация отчётов, main. Тесты — отдельный файл `scripts/test_benchmark_ollama.py` на `unittest`, покрывает чистые функции; HTTP / threading проверяются ручным smoke-test'ом.

**Tech Stack:** Python 3.10+ stdlib only. Зависимости НЕ добавляем. Тесты на `unittest`. Запуск тестов: `python3 -m unittest scripts.test_benchmark_ollama -v`.

**Спека:** см. `benchmark-plan.md` в корне репо. Все требования и формат отчётов — оттуда.
---
## Execution:
**Subagent-Driven (recommended)** — диспатчить свежий subagent на каждую Task, проверять между ними, быстрая итерация.

---

## File Structure

- **Create:** `scripts/benchmark-ollama.py` — единственный исполняемый файл.
- **Create:** `scripts/test_benchmark_ollama.py` — `unittest`-модуль для чистых функций.
- **Create:** `benchmark-results/` — каталог для отчётов (создаётся в runtime, в git не коммитим).
- **Modify:** `.gitignore` — добавить `benchmark-results/` (если ещё не игнорится).
- **Reference (read-only):** `scripts/test-ollama.py` — источник промпта, схемы, опций.
- **Reference (read-only):** `benchmark-plan.md` — спека.
- **Reference (read-only):** `transcript.json` — тестовые данные.

Скрипт делится на секции, разделённые комментариями `# === SECTION: <name> ===`:
1. Константы и схемы (переиспользованы из test-ollama.py).
2. Загрузка транскрипта и сборка промпта.
3. Метрики качества (чистые функции).
4. Ollama-клиент.
5. VRAM-монитор.
6. Прогресс-репортёр.
7. Runner одной модели.
8. Агрегация.
9. Генерация отчётов (JSON + Markdown).
10. Main / argparse.

Это позволяет легко находить и тестировать конкретный кусок без splitting на модули (что усложнило бы импорт из `scripts/` без `__init__.py`).

---

## Task 1: Скелет скрипта и переиспользованные константы

**Files:**
- Create: `scripts/benchmark-ollama.py`
- Create: `scripts/test_benchmark_ollama.py`
- Modify: `.gitignore`

- [ ] **Step 1: Создать `.gitignore`-исключение**

Прочитать существующий `.gitignore`, добавить (если отсутствует):

```
benchmark-results/
```

- [ ] **Step 2: Создать `scripts/benchmark-ollama.py` со скелетом**

```python
#!/usr/bin/env python3
"""Бенчмарк Ollama-моделей для yt-summary.

Прогоняет список моделей на одном транскрипте, измеряет время и ресурсы,
сохраняет сырые ответы для последующей LLM-as-judge оценки оркестратором.

Запуск:
  python3 scripts/benchmark-ollama.py
  python3 scripts/benchmark-ollama.py --runs 3 --output-dir benchmark-results/
  python3 scripts/benchmark-ollama.py --models-file models.txt

Спека: benchmark-plan.md в корне репо.
"""
import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# === SECTION: CONSTANTS ===

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TRANSCRIPT = ROOT / "transcript.json"
DEFAULT_OUTPUT_DIR = ROOT / "benchmark-results"
TRIM_LIMIT = 40000
RUNS_PER_MODEL = 3

DEFAULT_MODELS = [
    "qwen2.5:14b-instruct-q4_K_M",
    "qwen2.5:7b-instruct-q4_K_M",
    "llama3.1:8b-instruct-q4_K_M",
    "llama3.2:3b",
    "phi4:14b",
    "phi4-mini:3.8b",
    "cyberlis/saiga-mistral:7b-lora-q8_0",
    "gemma4:e4b",
]

KEYWORDS = [
    "Claude Code", "Seedance", "Kie.ai", "Nano Banana", "Vercel",
    "GitHub", "Higgsfield", "Visual Studio Code", "VS Code",
    "blueprint", "Aldworth", "architecture",
]

SYSTEM_PROMPT = """Ты ассистент, который делает подробный пересказ видео на YouTube по его субтитрам.
Всегда отвечай на русском языке.
Верни ТОЛЬКО валидный JSON со структурой:
{"main_idea": "строка", "summary": ["пункт 1", "пункт 2", "пункт 3", ...]}

Жёсткие требования к содержанию:
- В массиве summary ОБЯЗАТЕЛЬНО от 10 до 15 элементов. Меньше 10 — НЕДОПУСТИМО.
- Каждый элемент — законченное предложение длиной 100-200 символов с конкретикой: названия инструментов и сервисов, шаги, цифры, цены, технологии, имена.
- Покрой все ключевые этапы видео последовательно: что делает автор, какими инструментами пользуется, какие нюансы и советы даёт, какой итог.
- Никаких вступительных фраз вроде "В видео автор...". Никакого markdown, никаких ```json. Только сырой JSON."""

JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "main_idea": {"type": "string"},
        "summary": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 10,
            "maxItems": 15,
        },
    },
    "required": ["main_idea", "summary"],
}

OLLAMA_OPTIONS = {
    "temperature": 0.3,
    "num_ctx": 32768,
    "num_predict": 2000,
    "repeat_penalty": 1.3,
}


def main() -> int:
    print("benchmark-ollama: skeleton OK", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Создать `scripts/test_benchmark_ollama.py` со скелетом**

```python
"""Unit-тесты для чистых функций scripts/benchmark-ollama.py.

Запуск:
  python3 -m unittest scripts.test_benchmark_ollama -v
  или
  cd scripts && python3 -m unittest test_benchmark_ollama -v
"""
import importlib.util
import sys
import unittest
from pathlib import Path

# Загружаем модуль с дефисом в имени через importlib
_SPEC = importlib.util.spec_from_file_location(
    "benchmark_ollama",
    Path(__file__).resolve().parent / "benchmark-ollama.py",
)
bench = importlib.util.module_from_spec(_SPEC)
sys.modules["benchmark_ollama"] = bench
_SPEC.loader.exec_module(bench)


class TestSkeleton(unittest.TestCase):
    def test_constants_present(self):
        self.assertEqual(bench.RUNS_PER_MODEL, 3)
        self.assertEqual(bench.TRIM_LIMIT, 40000)
        self.assertIn("qwen2.5:14b-instruct-q4_K_M", bench.DEFAULT_MODELS)
        self.assertEqual(bench.JSON_SCHEMA["required"], ["main_idea", "summary"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 4: Запустить скрипт и тесты — убедиться что всё импортируется**

```bash
python3 scripts/benchmark-ollama.py
python3 -m unittest scripts.test_benchmark_ollama -v
```

Expected: скрипт печатает `benchmark-ollama: skeleton OK` и выходит с 0; тест `test_constants_present` проходит.

- [ ] **Step 5: Commit**

```bash
git add .gitignore scripts/benchmark-ollama.py scripts/test_benchmark_ollama.py
git commit -m "Add benchmark-ollama skeleton with reused constants"
```

---

## Task 2: Загрузка транскрипта и сборка промпта

**Files:**
- Modify: `scripts/benchmark-ollama.py` (добавить функции после секции CONSTANTS)
- Modify: `scripts/test_benchmark_ollama.py`

- [ ] **Step 1: Написать падающий тест**

Добавить в `test_benchmark_ollama.py`:

```python
class TestTranscriptHelpers(unittest.TestCase):
    def test_load_transcript_list_wrapper(self):
        # supadata возвращает список с одним объектом, как в transcript.json
        import json
        import tempfile
        from pathlib import Path

        data = [{"body": {"lang": "en", "content": "Hello world"}}]
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            path = Path(f.name)
        try:
            content, lang = bench.load_transcript(path)
            self.assertEqual(content, "Hello world")
            self.assertEqual(lang, "en")
        finally:
            path.unlink()

    def test_load_transcript_plain_object(self):
        import json
        import tempfile
        from pathlib import Path

        data = {"body": {"lang": "ru", "content": "Привет"}}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            path = Path(f.name)
        try:
            content, lang = bench.load_transcript(path)
            self.assertEqual(content, "Привет")
            self.assertEqual(lang, "ru")
        finally:
            path.unlink()

    def test_build_user_prompt_includes_title_and_content(self):
        prompt = bench.build_user_prompt("My Video", "Some content")
        self.assertIn("Название: My Video", prompt)
        self.assertIn("Some content", prompt)
        self.assertIn("10-15", prompt)
```

- [ ] **Step 2: Запустить тесты — убедиться, что падают**

```bash
python3 -m unittest scripts.test_benchmark_ollama.TestTranscriptHelpers -v
```

Expected: FAIL — `module 'benchmark_ollama' has no attribute 'load_transcript'`.

- [ ] **Step 3: Реализовать функции**

Добавить в `scripts/benchmark-ollama.py` после секции CONSTANTS:

```python
# === SECTION: TRANSCRIPT ===

def load_transcript(path: Path) -> tuple[str, Optional[str]]:
    """Загружает транскрипт в формате supadata (list[obj] или obj)."""
    data = json.loads(path.read_text())
    if isinstance(data, list):
        data = data[0]
    body = data.get("body", data)
    return body.get("content", ""), body.get("lang")


def build_user_prompt(title: str, content: str) -> str:
    """Идентично test-ollama.py build_user_prompt."""
    return (
        f"Название: {title}\n\n"
        f"Субтитры:\n{content}\n\n"
        "Напоминание: верни JSON с массивом summary из 10-15 подробных пунктов "
        "(по 100-200 символов), с конкретными названиями инструментов, шагами и цифрами."
    )
```

- [ ] **Step 4: Запустить тесты — убедиться что проходят**

```bash
python3 -m unittest scripts.test_benchmark_ollama -v
```

Expected: все тесты PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/benchmark-ollama.py scripts/test_benchmark_ollama.py
git commit -m "Add transcript loader and user prompt builder"
```

---

## Task 3: Метрики качества — формальные проверки

**Files:**
- Modify: `scripts/benchmark-ollama.py`
- Modify: `scripts/test_benchmark_ollama.py`

- [ ] **Step 1: Написать падающие тесты**

```python
class TestFormalMetrics(unittest.TestCase):
    def _good(self):
        return {
            "main_idea": "О чём видео",
            "summary": ["A" * 150 for _ in range(12)],
        }

    def test_perfect_response(self):
        m = bench.compute_formal_metrics(json.dumps(self._good()))
        self.assertTrue(m["json_valid"])
        self.assertTrue(m["has_required_fields"])
        self.assertEqual(m["summary_count"], 12)
        self.assertTrue(m["count_in_range"])
        self.assertEqual(m["too_short"], 0)
        self.assertEqual(m["too_long"], 0)
        self.assertEqual(m["length_ok_ratio"], 1.0)

    def test_invalid_json(self):
        m = bench.compute_formal_metrics("{not json")
        self.assertFalse(m["json_valid"])
        self.assertFalse(m["has_required_fields"])
        self.assertEqual(m["summary_count"], 0)
        self.assertFalse(m["count_in_range"])
        self.assertEqual(m["length_ok_ratio"], 0.0)

    def test_missing_summary(self):
        m = bench.compute_formal_metrics(json.dumps({"main_idea": "x"}))
        self.assertTrue(m["json_valid"])
        self.assertFalse(m["has_required_fields"])
        self.assertEqual(m["summary_count"], 0)

    def test_count_out_of_range(self):
        d = {"main_idea": "x", "summary": ["A" * 150 for _ in range(5)]}
        m = bench.compute_formal_metrics(json.dumps(d))
        self.assertEqual(m["summary_count"], 5)
        self.assertFalse(m["count_in_range"])

    def test_mixed_lengths(self):
        d = {
            "main_idea": "x",
            "summary": ["A" * 50] * 2 + ["A" * 150] * 8 + ["A" * 250] * 2,
        }
        m = bench.compute_formal_metrics(json.dumps(d))
        self.assertEqual(m["too_short"], 2)
        self.assertEqual(m["too_long"], 2)
        self.assertEqual(m["length_ok_ratio"], 8 / 12)

    def test_summary_not_list(self):
        d = {"main_idea": "x", "summary": "not a list"}
        m = bench.compute_formal_metrics(json.dumps(d))
        self.assertFalse(m["has_required_fields"])
        self.assertEqual(m["summary_count"], 0)
```

- [ ] **Step 2: Запустить — должны упасть**

```bash
python3 -m unittest scripts.test_benchmark_ollama.TestFormalMetrics -v
```

Expected: FAIL — `compute_formal_metrics` не определена.

- [ ] **Step 3: Реализовать**

Добавить в скрипт:

```python
# === SECTION: METRICS ===

def compute_formal_metrics(raw: str) -> dict:
    """Формальные проверки ответа модели.

    Возвращает dict со всеми ключами всегда (даже при невалидном JSON),
    чтобы downstream-код не падал на отсутствующих полях.
    """
    out = {
        "json_valid": False,
        "has_required_fields": False,
        "summary_count": 0,
        "count_in_range": False,
        "too_short": 0,
        "too_long": 0,
        "length_ok_ratio": 0.0,
    }
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return out
    out["json_valid"] = True
    if not isinstance(parsed, dict):
        return out
    main_idea = parsed.get("main_idea")
    summary = parsed.get("summary")
    if not isinstance(main_idea, str) or not isinstance(summary, list):
        return out
    out["has_required_fields"] = True
    out["summary_count"] = len(summary)
    out["count_in_range"] = 10 <= len(summary) <= 15
    if not summary:
        return out
    str_items = [s for s in summary if isinstance(s, str)]
    out["too_short"] = sum(1 for s in str_items if len(s) < 100)
    out["too_long"] = sum(1 for s in str_items if len(s) > 200)
    in_range = sum(1 for s in str_items if 100 <= len(s) <= 200)
    out["length_ok_ratio"] = in_range / len(summary)
    return out
```

- [ ] **Step 4: Запустить — должны пройти**

```bash
python3 -m unittest scripts.test_benchmark_ollama -v
```

Expected: все PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/benchmark-ollama.py scripts/test_benchmark_ollama.py
git commit -m "Add formal quality metrics for model responses"
```

---

## Task 4: Метрики качества — content heuristics

**Files:**
- Modify: `scripts/benchmark-ollama.py`
- Modify: `scripts/test_benchmark_ollama.py`

- [ ] **Step 1: Написать падающие тесты**

```python
class TestContentHeuristics(unittest.TestCase):
    def test_keyword_coverage_full(self):
        summary = [
            "Использовали Claude Code и Vercel для деплоя.",
            "Открыли Visual Studio Code и поставили GitHub.",
        ]
        cov = bench.keyword_coverage(summary, ["Claude Code", "Vercel", "GitHub", "VS Code"])
        # 3 из 4 (VS Code не встречается, Visual Studio Code не равно VS Code)
        self.assertAlmostEqual(cov, 0.75, places=2)

    def test_keyword_coverage_case_insensitive(self):
        cov = bench.keyword_coverage(["claude code rocks"], ["Claude Code"])
        self.assertEqual(cov, 1.0)

    def test_keyword_coverage_empty_summary(self):
        self.assertEqual(bench.keyword_coverage([], ["X"]), 0.0)

    def test_keyword_coverage_empty_keywords(self):
        self.assertEqual(bench.keyword_coverage(["text"], []), 0.0)

    def test_specificity_with_digits(self):
        items = ["Стоимость 25 долларов в месяц.", "Просто текст без ничего."]
        ratio = bench.specificity_ratio(items)
        self.assertEqual(ratio, 0.5)

    def test_specificity_with_proper_nouns(self):
        items = ["Использовали Claude для работы.", "обычный текст без названий"]
        ratio = bench.specificity_ratio(items)
        self.assertEqual(ratio, 0.5)

    def test_specificity_empty(self):
        self.assertEqual(bench.specificity_ratio([]), 0.0)

    def test_duplicate_summary_keys(self):
        raw = '{"summary": ["a"], "main_idea": "x", "summary": ["b"]}'
        self.assertEqual(bench.duplicate_summary_keys(raw), 2)
        self.assertEqual(bench.duplicate_summary_keys('{"summary": []}'), 1)
```

- [ ] **Step 2: Запустить — упадут**

- [ ] **Step 3: Реализовать**

Добавить в секцию METRICS:

```python
# Слово с заглавной латинской буквой длиной >2 (для детекта proper nouns).
# Кириллица намеренно не включена — для русского текста заглавная буква
# часто означает просто начало предложения, не имя собственное.
_PROPER_NOUN_RE = re.compile(r"\b[A-Z][a-zA-Z]{2,}\b")
_DIGIT_RE = re.compile(r"\d")


def keyword_coverage(summary: list[str], keywords: list[str]) -> float:
    if not summary or not keywords:
        return 0.0
    blob = " ".join(s for s in summary if isinstance(s, str)).lower()
    hits = sum(1 for kw in keywords if kw.lower() in blob)
    return hits / len(keywords)


def specificity_ratio(summary: list[str]) -> float:
    if not summary:
        return 0.0
    str_items = [s for s in summary if isinstance(s, str)]
    if not str_items:
        return 0.0
    specific = sum(
        1 for s in str_items
        if _DIGIT_RE.search(s) or _PROPER_NOUN_RE.search(s)
    )
    return specific / len(str_items)


def duplicate_summary_keys(raw: str) -> int:
    """Считает вхождения паттерна `"summary"` в сыром JSON-тексте.

    >1 — модель сгенерировала повторяющийся ключ (известная патология,
    второй ключ при стандартном json.loads перезаписывает первый — теряется data).
    """
    return raw.count('"summary"')
```

- [ ] **Step 4: Запустить — пройдут**

- [ ] **Step 5: Commit**

```bash
git add scripts/benchmark-ollama.py scripts/test_benchmark_ollama.py
git commit -m "Add content heuristics: keyword coverage, specificity, duplicate keys"
```

---

## Task 5: Quality score и полный compute_metrics

**Files:**
- Modify: `scripts/benchmark-ollama.py`
- Modify: `scripts/test_benchmark_ollama.py`

- [ ] **Step 1: Написать падающие тесты**

```python
class TestQualityScore(unittest.TestCase):
    def test_perfect_score(self):
        m = {
            "json_valid": True,
            "has_required_fields": True,
            "count_in_range": True,
            "length_ok_ratio": 1.0,
            "keyword_coverage": 1.0,
            "specificity_ratio": 1.0,
        }
        self.assertEqual(bench.compute_quality_score(m), 100.0)

    def test_zero_on_invalid_json(self):
        m = {
            "json_valid": False,
            "has_required_fields": False,
            "count_in_range": False,
            "length_ok_ratio": 0.0,
            "keyword_coverage": 0.0,
            "specificity_ratio": 0.0,
        }
        self.assertEqual(bench.compute_quality_score(m), 0.0)

    def test_partial(self):
        m = {
            "json_valid": True,            # 20
            "has_required_fields": True,   # 10
            "count_in_range": False,       # 0
            "length_ok_ratio": 0.5,        # 7.5
            "keyword_coverage": 0.5,       # 12.5
            "specificity_ratio": 0.5,      # 7.5
        }
        self.assertEqual(bench.compute_quality_score(m), 57.5)


class TestComputeMetrics(unittest.TestCase):
    def test_combines_formal_and_content(self):
        d = {
            "main_idea": "x",
            "summary": [
                f"Пункт {i} использует Claude Code и Vercel со ссылкой на 2026 год для деплоя проекта."
                for i in range(12)
            ],
        }
        raw = json.dumps(d, ensure_ascii=False)
        m = bench.compute_metrics(raw, bench.KEYWORDS)
        # Формальные
        self.assertTrue(m["json_valid"])
        self.assertEqual(m["summary_count"], 12)
        # Контентные
        self.assertGreater(m["keyword_coverage"], 0.0)
        self.assertEqual(m["specificity_ratio"], 1.0)
        self.assertEqual(m["duplicate_summary_keys"], 1)
        # Score
        self.assertGreater(m["quality_score"], 0.0)
```

- [ ] **Step 2: Запустить — упадут**

- [ ] **Step 3: Реализовать**

```python
def compute_quality_score(metrics: dict) -> float:
    """Интегральный балл 0-100. Веса см. benchmark-plan.md."""
    score = (
        20 * float(metrics.get("json_valid", False))
        + 10 * float(metrics.get("has_required_fields", False))
        + 15 * float(metrics.get("count_in_range", False))
        + 15 * float(metrics.get("length_ok_ratio", 0.0))
        + 25 * float(metrics.get("keyword_coverage", 0.0))
        + 15 * float(metrics.get("specificity_ratio", 0.0))
    )
    return round(score, 2)


def compute_metrics(raw: str, keywords: list[str]) -> dict:
    """Полный набор метрик для одного ответа модели."""
    m = compute_formal_metrics(raw)
    summary = []
    if m["json_valid"]:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and isinstance(parsed.get("summary"), list):
                summary = parsed["summary"]
        except json.JSONDecodeError:
            pass
    m["keyword_coverage"] = keyword_coverage(summary, keywords)
    m["specificity_ratio"] = specificity_ratio(summary)
    m["duplicate_summary_keys"] = duplicate_summary_keys(raw)
    m["quality_score"] = compute_quality_score(m)
    return m
```

- [ ] **Step 4: Запустить — пройдут**

- [ ] **Step 5: Commit**

```bash
git add scripts/benchmark-ollama.py scripts/test_benchmark_ollama.py
git commit -m "Add quality_score and unified compute_metrics"
```

---

## Task 6: Ollama-клиент — chat и ps

**Files:**
- Modify: `scripts/benchmark-ollama.py`

Эти функции делают HTTP — юнит-тесты с моками громоздки. Покрываем их smoke-тестом в Task 14. Здесь — реализация.

- [ ] **Step 1: Реализовать**

```python
# === SECTION: OLLAMA CLIENT ===

class OllamaError(RuntimeError):
    pass


def _http_post(url: str, payload: dict, timeout: float = 600) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise OllamaError(f"HTTP {e.code} from {url}: {e.read().decode('utf-8', 'replace')}")
    except urllib.error.URLError as e:
        raise OllamaError(f"Network error to {url}: {e.reason}")


def _http_get(url: str, timeout: float = 30) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise OllamaError(f"HTTP {e.code} from {url}")
    except urllib.error.URLError as e:
        raise OllamaError(f"Network error to {url}: {e.reason}")


def ollama_chat(base_url: str, model: str, messages: list[dict],
                options: dict, fmt, keep_alive: str = "5m",
                timeout: float = 600) -> dict:
    """Один запрос к /api/chat с stream=False. Возвращает полный JSON ответа."""
    payload = {
        "model": model,
        "stream": False,
        "format": fmt,
        "keep_alive": keep_alive,
        "options": options,
        "messages": messages,
    }
    return _http_post(f"{base_url}/api/chat", payload, timeout=timeout)


def ollama_ps(base_url: str) -> list[dict]:
    """Возвращает список загруженных моделей с полями size, size_vram, name."""
    data = _http_get(f"{base_url}/api/ps")
    return data.get("models", [])
```

- [ ] **Step 2: Запустить существующие тесты — ничего не сломалось**

```bash
python3 -m unittest scripts.test_benchmark_ollama -v
```

Expected: все PASS.

- [ ] **Step 3: Smoke-тест вручную (требует запущенного Ollama)**

```bash
python3 -c "
import importlib.util, sys
spec = importlib.util.spec_from_file_location('b', 'scripts/benchmark-ollama.py')
b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
print(b.ollama_ps('http://localhost:11434'))
"
```

Expected: список словарей (возможно пустой). Если Ollama не запущен — `OllamaError`.

- [ ] **Step 4: Commit**

```bash
git add scripts/benchmark-ollama.py
git commit -m "Add ollama HTTP client (chat, ps) with error wrapping"
```

---

## Task 7: Ollama-клиент — pull с прогресс-выводом

**Files:**
- Modify: `scripts/benchmark-ollama.py`
- Modify: `scripts/test_benchmark_ollama.py`

- [ ] **Step 1: Написать падающий тест на парсер прогресс-стрима**

```python
class TestPullProgressParser(unittest.TestCase):
    def test_parse_pull_lines(self):
        # Эмулируем тело /api/pull (NDJSON)
        lines = [
            '{"status":"pulling manifest"}',
            '{"status":"downloading","digest":"sha256:abc","total":100,"completed":40}',
            '{"status":"downloading","digest":"sha256:abc","total":100,"completed":100}',
            '{"status":"success"}',
        ]
        events = list(bench._iter_pull_events(iter(line.encode() for line in lines)))
        self.assertEqual(events[0]["status"], "pulling manifest")
        self.assertEqual(events[1]["completed"], 40)
        self.assertEqual(events[-1]["status"], "success")
```

- [ ] **Step 2: Запустить — упадёт**

- [ ] **Step 3: Реализовать pull**

Добавить в секцию OLLAMA CLIENT:

```python
def _iter_pull_events(line_iter):
    """Парсит NDJSON-стрим от /api/pull. line_iter выдаёт bytes."""
    for raw in line_iter:
        if not raw:
            continue
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        raw = raw.strip()
        if not raw:
            continue
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            continue


def ollama_pull(base_url: str, model: str,
                on_progress: Optional[Callable[[dict], None]] = None,
                timeout: float = 1800) -> None:
    """Скачивает модель через /api/pull, передаёт каждый event в on_progress.

    Бросает OllamaError если последний event не имеет status='success'.
    """
    payload = {"name": model, "stream": True}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/pull",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    last = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for event in _iter_pull_events(resp):
                last = event
                if on_progress:
                    on_progress(event)
                if event.get("error"):
                    raise OllamaError(f"pull {model}: {event['error']}")
    except urllib.error.HTTPError as e:
        raise OllamaError(f"pull {model}: HTTP {e.code}")
    except urllib.error.URLError as e:
        raise OllamaError(f"pull {model}: {e.reason}")
    if not last or last.get("status") != "success":
        raise OllamaError(f"pull {model}: stream ended without success (last={last})")
```

- [ ] **Step 4: Запустить тесты — пройдут**

- [ ] **Step 5: Commit**

```bash
git add scripts/benchmark-ollama.py scripts/test_benchmark_ollama.py
git commit -m "Add ollama pull with NDJSON progress streaming"
```

---

## Task 8: Ollama-клиент — unload с поллингом

**Files:**
- Modify: `scripts/benchmark-ollama.py`

- [ ] **Step 1: Реализовать**

Добавить в секцию OLLAMA CLIENT:

```python
def ollama_unload(base_url: str, model: str, timeout: float = 60) -> bool:
    """Выгружает модель через keep_alive=0 + поллит /api/ps до пропадания.

    Возвращает True если модель выгружена в пределах timeout, иначе False.
    """
    # Шаг 1: послать запрос с keep_alive=0
    try:
        _http_post(
            f"{base_url}/api/chat",
            {
                "model": model,
                "stream": False,
                "keep_alive": 0,
                "messages": [{"role": "user", "content": "."}],
            },
            timeout=30,
        )
    except OllamaError:
        # Модель может уже быть не в памяти — это ок
        pass

    # Шаг 2: поллить /api/ps
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            loaded = ollama_ps(base_url)
        except OllamaError:
            return False
        if not any(m.get("name") == model or m.get("model") == model for m in loaded):
            return True
        time.sleep(1.0)
    return False


def ollama_unload_all(base_url: str, timeout: float = 60) -> None:
    """Выгружает все загруженные сейчас модели (используется в pre-run check)."""
    try:
        loaded = ollama_ps(base_url)
    except OllamaError:
        return
    for m in loaded:
        name = m.get("name") or m.get("model")
        if name:
            ollama_unload(base_url, name, timeout=timeout)
```

- [ ] **Step 2: Тесты не упали**

```bash
python3 -m unittest scripts.test_benchmark_ollama -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add scripts/benchmark-ollama.py
git commit -m "Add ollama_unload with /api/ps polling and unload_all helper"
```

---

## Task 9: VRAM-монитор через nvidia-smi

**Files:**
- Modify: `scripts/benchmark-ollama.py`

- [ ] **Step 1: Реализовать**

Добавить новую секцию:

```python
# === SECTION: VRAM MONITOR ===

class NvidiaSmiMonitor:
    """Контекстный менеджер: фоновый поток сэмплит nvidia-smi memory.used.

    Использование:
        with NvidiaSmiMonitor() as mon:
            ...inference...
        peak = mon.peak_mb  # None если nvidia-smi недоступен
    """

    SAMPLE_INTERVAL = 0.5

    def __init__(self):
        self.peak_mb: Optional[int] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._available = self._check()

    @staticmethod
    def _check() -> bool:
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2,
            )
            return r.returncode == 0
        except (FileNotFoundError, subprocess.SubprocessError):
            return False

    def _sample_once(self) -> Optional[int]:
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2,
            )
            if r.returncode != 0:
                return None
            # Может быть несколько GPU — берём максимум
            values = [int(line.strip()) for line in r.stdout.splitlines() if line.strip()]
            return max(values) if values else None
        except (subprocess.SubprocessError, ValueError):
            return None

    def _loop(self):
        while not self._stop.is_set():
            v = self._sample_once()
            if v is not None:
                self.peak_mb = v if self.peak_mb is None else max(self.peak_mb, v)
            self._stop.wait(self.SAMPLE_INTERVAL)

    def __enter__(self):
        if self._available:
            self._stop.clear()
            self.peak_mb = None
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc):
        if self._thread:
            self._stop.set()
            self._thread.join(timeout=2)
```

- [ ] **Step 2: Smoke-тест**

```bash
python3 -c "
import importlib.util, time
spec = importlib.util.spec_from_file_location('b', 'scripts/benchmark-ollama.py')
b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
with b.NvidiaSmiMonitor() as mon:
    time.sleep(2)
print('peak_mb:', mon.peak_mb)
"
```

Expected: число в МБ (если есть GPU) или `None` (если nvidia-smi недоступен).

- [ ] **Step 3: Commit**

```bash
git add scripts/benchmark-ollama.py
git commit -m "Add NvidiaSmiMonitor for peak VRAM sampling"
```

---

## Task 10: Прогресс-репортёр

**Files:**
- Modify: `scripts/benchmark-ollama.py`
- Modify: `scripts/test_benchmark_ollama.py`

- [ ] **Step 1: Написать тесты для не-TTY режима**

```python
class TestProgressReporterNonTTY(unittest.TestCase):
    def setUp(self):
        import io
        self.buf = io.StringIO()
        self.reporter = bench.ProgressReporter(stream=self.buf, is_tty=False)

    def test_overall_start(self):
        self.reporter.overall_start(total_models=3, runs_per_model=3)
        out = self.buf.getvalue()
        self.assertIn("[BENCH]", out)
        self.assertIn("3 моделей", out)
        self.assertIn("4 прогона", out)  # warm-up + 3

    def test_model_lifecycle(self):
        self.reporter.model_start(idx=1, total=3, model="qwen2.5:7b")
        self.reporter.phase("loading")
        self.reporter.phase_done(elapsed=12.5)
        self.reporter.run_done(idx=1, total=3, elapsed=8.0, tokens_per_sec=42.0)
        self.reporter.model_done(score=82.5, inf=8.7, vram=9120, cov=0.75)
        out = self.buf.getvalue()
        self.assertIn("[1/3] qwen2.5:7b", out)
        self.assertIn("loading", out)
        self.assertIn("12.5", out)
        self.assertIn("run 1/3", out)
        self.assertIn("42", out)
        self.assertIn("score=82.5", out)

    def test_overall_done(self):
        self.reporter.overall_done(elapsed=2832, ok=8, skipped=1, unusable=1, report_path="x.md")
        out = self.buf.getvalue()
        self.assertIn("47:12", out)
        self.assertIn("OK=8", out)
        self.assertIn("SKIPPED=1", out)
        self.assertIn("UNUSABLE=1", out)
        self.assertIn("x.md", out)
```

- [ ] **Step 2: Запустить — упадут**

- [ ] **Step 3: Реализовать**

Добавить новую секцию:

```python
# === SECTION: PROGRESS ===

def _fmt_mmss(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60:02d}:{s % 60:02d}"


class ProgressReporter:
    """Прогресс-индикатор в stderr.

    TTY-режим: фоновый таймер обновляет текущую строку через \r каждые ~2с.
    Не-TTY: одна строка на событие, без перезаписи.
    """

    TICK_INTERVAL = 2.0

    def __init__(self, stream=None, is_tty: Optional[bool] = None):
        self.stream = stream if stream is not None else sys.stderr
        self.is_tty = is_tty if is_tty is not None else self.stream.isatty()
        self._tick_stop: Optional[threading.Event] = None
        self._tick_thread: Optional[threading.Thread] = None
        self._current_phase: Optional[str] = None
        self._phase_start: float = 0.0
        self._lock = threading.Lock()

    def _write(self, s: str, end: str = "\n"):
        with self._lock:
            self.stream.write(s + end)
            self.stream.flush()

    def _write_inline(self, s: str):
        with self._lock:
            self.stream.write("\r" + s.ljust(80))
            self.stream.flush()

    def overall_start(self, total_models: int, runs_per_model: int):
        per_model = runs_per_model + 1  # warm-up + N
        total = total_models * per_model
        self._write(
            f"[BENCH] {total_models} моделей × {per_model} прогона "
            f"(warm-up + {runs_per_model}) = {total} запросов"
        )

    def model_start(self, idx: int, total: int, model: str):
        self._write(f"[{idx}/{total}] {model}")

    def phase(self, name: str):
        self._stop_tick()
        self._current_phase = name
        self._phase_start = time.monotonic()
        if self.is_tty:
            self._start_tick()
        else:
            self._write(f"  {name}…")

    def phase_done(self, elapsed: Optional[float] = None):
        self._stop_tick()
        if elapsed is None:
            elapsed = time.monotonic() - self._phase_start
        line = f"  {self._current_phase} · {_fmt_mmss(elapsed)}"
        if self.is_tty:
            self._write_inline(line)
            self._write("")  # перевод строки
        else:
            self._write(line)
        self._current_phase = None

    def run_done(self, idx: int, total: int, elapsed: float, tokens_per_sec: Optional[float]):
        self._stop_tick()
        tps = f" ({tokens_per_sec:.0f} tok/s)" if tokens_per_sec else ""
        line = f"  run {idx}/{total} · {_fmt_mmss(elapsed)}{tps}"
        if self.is_tty:
            self._write_inline(line)
            self._write("")
        else:
            self._write(line)

    def model_done(self, score: float, inf: float, vram: Optional[int], cov: float):
        vram_str = f"{vram}MB" if vram is not None else "n/a"
        self._write(
            f"  ✔ score={score:.1f}  inf={inf:.1f}s  vram={vram_str}  cov={cov:.2f}"
        )

    def model_skipped(self, reason: str):
        self._write(f"  ⚠ SKIPPED: {reason}")

    def overall_done(self, elapsed: float, ok: int, skipped: int, unusable: int, report_path: str):
        self._write(
            f"[BENCH] готово за {_fmt_mmss(elapsed)} · "
            f"OK={ok} SKIPPED={skipped} UNUSABLE={unusable} · "
            f"отчёт: {report_path}"
        )

    def _start_tick(self):
        self._tick_stop = threading.Event()
        self._tick_thread = threading.Thread(target=self._tick_loop, daemon=True)
        self._tick_thread.start()

    def _stop_tick(self):
        if self._tick_stop:
            self._tick_stop.set()
        if self._tick_thread:
            self._tick_thread.join(timeout=1)
        self._tick_stop = None
        self._tick_thread = None

    def _tick_loop(self):
        while self._tick_stop and not self._tick_stop.is_set():
            elapsed = time.monotonic() - self._phase_start
            self._write_inline(f"  {self._current_phase}… {_fmt_mmss(elapsed)}")
            self._tick_stop.wait(self.TICK_INTERVAL)
```

- [ ] **Step 4: Запустить тесты — пройдут**

- [ ] **Step 5: Commit**

```bash
git add scripts/benchmark-ollama.py scripts/test_benchmark_ollama.py
git commit -m "Add ProgressReporter with TTY/non-TTY modes"
```

---

## Task 11: Runner одной модели

**Files:**
- Modify: `scripts/benchmark-ollama.py`

Без юнит-тестов — это оркестрация HTTP. Smoke в Task 14.

- [ ] **Step 1: Реализовать**

Добавить новую секцию:

```python
# === SECTION: MODEL RUNNER ===

def _ns_to_s(ns: Optional[int]) -> Optional[float]:
    return ns / 1e9 if ns else None


def _do_one_run(base_url: str, model: str, messages: list[dict],
                fmt, keepalive: str, timeout: float,
                keywords: list[str]) -> dict:
    """Делает один запрос + собирает все метрики прогона."""
    with NvidiaSmiMonitor() as vram_mon:
        t0 = time.monotonic()
        resp = ollama_chat(base_url, model, messages, OLLAMA_OPTIONS, fmt,
                           keep_alive=keepalive, timeout=timeout)
        wall_s = time.monotonic() - t0
        try:
            ps = ollama_ps(base_url)
        except OllamaError:
            ps = []
    raw = resp.get("message", {}).get("content", "")
    metrics = compute_metrics(raw, keywords)
    ps_entry = next(
        (m for m in ps if (m.get("name") == model or m.get("model") == model)),
        {},
    )
    size_total = ps_entry.get("size")
    size_vram = ps_entry.get("size_vram")
    return {
        "wall_s": wall_s,
        "load_time_s": _ns_to_s(resp.get("load_duration")),
        "prompt_eval_s": _ns_to_s(resp.get("prompt_eval_duration")),
        "inference_s": _ns_to_s(resp.get("eval_duration")),
        "total_time_s": _ns_to_s(resp.get("total_duration")),
        "eval_count": resp.get("eval_count"),
        "tokens_per_sec": (
            resp.get("eval_count") / (_ns_to_s(resp.get("eval_duration")) or 1e-9)
            if resp.get("eval_count") and resp.get("eval_duration") else None
        ),
        "size_total_mb": round(size_total / 1048576, 1) if size_total else None,
        "size_vram_mb": round(size_vram / 1048576, 1) if size_vram else None,
        "vram_peak_mb": vram_mon.peak_mb,
        "raw_response": raw,
        "metrics": metrics,
    }


def run_model(base_url: str, model: str, idx: int, total: int,
              messages: list[dict], runs: int,
              keywords: list[str], reporter: ProgressReporter,
              per_model_dir: Path) -> dict:
    """Полный цикл по одной модели. Возвращает словарь, готовый для отчёта."""
    reporter.model_start(idx, total, model)
    warnings: list[str] = []

    # 1. Pull
    reporter.phase("pulling")
    try:
        ollama_pull(base_url, model)
        reporter.phase_done()
    except OllamaError as e:
        reporter.phase_done()
        reporter.model_skipped(str(e))
        return _skipped(model, f"pull failed: {e}")

    # 2. Pre-run cleanup
    reporter.phase("cleanup")
    ollama_unload_all(base_url, timeout=30)
    reporter.phase_done()

    # 3. Warm-up
    reporter.phase("warm-up")
    try:
        warm_resp = ollama_chat(
            base_url, model, messages, OLLAMA_OPTIONS, JSON_SCHEMA,
            keep_alive=os.environ.get("OLLAMA_KEEP_ALIVE_ACTIVE", "5m"),
            timeout=600,
        )
        warm_raw = warm_resp.get("message", {}).get("content", "")
        try:
            json.loads(warm_raw)
        except json.JSONDecodeError:
            reporter.phase_done()
            reporter.model_skipped("warm-up returned invalid JSON")
            return _skipped(model, "warm-up returned invalid JSON")
        reporter.phase_done()
    except OllamaError as e:
        reporter.phase_done()
        reporter.model_skipped(f"warm-up failed: {e}")
        return _skipped(model, f"warm-up failed: {e}")

    # 4. Measured runs
    runs_data = []
    for i in range(1, runs + 1):
        reporter.phase(f"run {i}/{runs}")
        try:
            r = _do_one_run(
                base_url, model, messages, JSON_SCHEMA,
                keepalive=os.environ.get("OLLAMA_KEEP_ALIVE_ACTIVE", "5m"),
                timeout=600, keywords=keywords,
            )
        except OllamaError as e:
            reporter.phase_done()
            warnings.append(f"run {i} failed: {e}")
            continue
        runs_data.append(r)
        reporter.run_done(i, runs, r["wall_s"], r["tokens_per_sec"])

    # 5. Unload
    reporter.phase("unload")
    ok = ollama_unload(base_url, model, timeout=60)
    reporter.phase_done()
    if not ok:
        warnings.append("unload timeout — модель не пропала из /api/ps за 60с")

    # 6. Aggregate
    if not runs_data:
        reporter.model_skipped("все measured runs упали")
        return _skipped(model, "all measured runs failed")
    aggregated = aggregate_runs(runs_data)
    status = "UNUSABLE" if aggregated["json_valid_majority"] is False else "OK"

    result = {
        "model": model,
        "status": status,
        "runs": runs_data,
        "aggregated": aggregated,
        "warnings": warnings,
    }

    # 7. Persist per-model JSON immediately
    per_model_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "_", model)
    (per_model_dir / f"{slug}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2)
    )

    reporter.model_done(
        score=aggregated.get("quality_score_median", 0.0),
        inf=aggregated.get("inference_s_median", 0.0),
        vram=aggregated.get("vram_peak_mb_median"),
        cov=aggregated.get("keyword_coverage_median", 0.0),
    )
    return result


def _skipped(model: str, reason: str) -> dict:
    return {
        "model": model,
        "status": "SKIPPED",
        "runs": [],
        "aggregated": {},
        "warnings": [reason],
    }
```

- [ ] **Step 2: Тесты не упали**

```bash
python3 -m unittest scripts.test_benchmark_ollama -v
```

Expected: PASS (новых тестов не добавили, существующие не должны сломаться).

- [ ] **Step 3: Commit**

```bash
git add scripts/benchmark-ollama.py
git commit -m "Add per-model runner with full pull/warm-up/runs/unload cycle"
```

---

## Task 12: Агрегация прогонов

**Files:**
- Modify: `scripts/benchmark-ollama.py`
- Modify: `scripts/test_benchmark_ollama.py`

- [ ] **Step 1: Написать падающие тесты**

```python
class TestAggregateRuns(unittest.TestCase):
    def _run(self, inf_s, score, vram, cov, json_valid=True):
        return {
            "wall_s": inf_s + 1,
            "load_time_s": 5.0,
            "prompt_eval_s": 0.5,
            "inference_s": inf_s,
            "total_time_s": inf_s + 0.5,
            "eval_count": 100,
            "tokens_per_sec": 100 / inf_s if inf_s else None,
            "size_total_mb": 9000.0,
            "size_vram_mb": 9000.0,
            "vram_peak_mb": vram,
            "raw_response": "{}",
            "metrics": {
                "json_valid": json_valid,
                "has_required_fields": json_valid,
                "summary_count": 12,
                "count_in_range": True,
                "too_short": 0,
                "too_long": 0,
                "length_ok_ratio": 1.0,
                "keyword_coverage": cov,
                "specificity_ratio": 0.5,
                "duplicate_summary_keys": 1,
                "quality_score": score,
            },
        }

    def test_median_of_three(self):
        runs = [self._run(8, 70, 9000, 0.6), self._run(10, 80, 9100, 0.7), self._run(9, 75, 9050, 0.65)]
        agg = bench.aggregate_runs(runs)
        self.assertEqual(agg["inference_s_median"], 9.0)
        self.assertEqual(agg["quality_score_median"], 75.0)
        self.assertEqual(agg["vram_peak_mb_median"], 9050)
        self.assertAlmostEqual(agg["keyword_coverage_median"], 0.65, places=2)
        self.assertTrue(agg["json_valid_majority"])
        self.assertEqual(agg["json_valid_all_runs"], True)

    def test_majority_invalid(self):
        runs = [
            self._run(8, 0, 9000, 0, json_valid=False),
            self._run(8, 0, 9000, 0, json_valid=False),
            self._run(8, 80, 9000, 0.7, json_valid=True),
        ]
        agg = bench.aggregate_runs(runs)
        self.assertFalse(agg["json_valid_majority"])
        self.assertFalse(agg["json_valid_all_runs"])

    def test_handles_none_vram(self):
        runs = [self._run(8, 70, None, 0.6), self._run(8, 70, None, 0.6)]
        agg = bench.aggregate_runs(runs)
        self.assertIsNone(agg["vram_peak_mb_median"])
```

- [ ] **Step 2: Запустить — упадут**

- [ ] **Step 3: Реализовать**

Добавить новую секцию:

```python
# === SECTION: AGGREGATION ===

def _median(values: list, default=None):
    cleaned = [v for v in values if v is not None]
    if not cleaned:
        return default
    return statistics.median(cleaned)


def aggregate_runs(runs: list[dict]) -> dict:
    """Сворачивает список runs в одну сводку с медианой и флагами валидности."""
    metric_keys = ["quality_score", "keyword_coverage", "specificity_ratio", "length_ok_ratio"]
    perf_keys = ["load_time_s", "prompt_eval_s", "inference_s", "total_time_s",
                 "tokens_per_sec", "size_total_mb", "size_vram_mb", "vram_peak_mb"]

    out: dict[str, Any] = {}
    for k in perf_keys:
        out[f"{k}_median"] = _median([r.get(k) for r in runs])
    for k in metric_keys:
        out[f"{k}_median"] = _median([r["metrics"].get(k) for r in runs])

    valid_flags = [r["metrics"].get("json_valid", False) for r in runs]
    out["json_valid_all_runs"] = all(valid_flags)
    out["json_valid_majority"] = sum(valid_flags) > len(valid_flags) / 2

    # Min/max для inference и vram (полезно для отчёта)
    inf_values = [r.get("inference_s") for r in runs if r.get("inference_s") is not None]
    out["inference_s_min"] = min(inf_values) if inf_values else None
    out["inference_s_max"] = max(inf_values) if inf_values else None

    return out
```

- [ ] **Step 4: Запустить тесты — пройдут**

- [ ] **Step 5: Commit**

```bash
git add scripts/benchmark-ollama.py scripts/test_benchmark_ollama.py
git commit -m "Add aggregate_runs with median/min/max and validity flags"
```

---

## Task 13: Запись отчётов (JSON + Markdown)

**Files:**
- Modify: `scripts/benchmark-ollama.py`
- Modify: `scripts/test_benchmark_ollama.py`

- [ ] **Step 1: Написать падающие тесты**

```python
class TestReportWriters(unittest.TestCase):
    def _result(self, model, score, status="OK"):
        return {
            "model": model,
            "status": status,
            "runs": [],
            "aggregated": {
                "quality_score_median": score,
                "inference_s_median": 8.7,
                "tokens_per_sec_median": 42.0,
                "size_vram_mb_median": 9120,
                "size_total_mb_median": 9120,
                "vram_peak_mb_median": 9800,
                "keyword_coverage_median": 0.75,
                "specificity_ratio_median": 0.66,
                "json_valid_all_runs": True,
                "json_valid_majority": True,
            },
            "warnings": [],
        }

    def test_json_round_trip(self):
        import tempfile
        from pathlib import Path
        meta = {"runs_per_model": 3, "started_at": "2026-04-18T16:30:00Z"}
        results = [self._result("a", 80), self._result("b", 70)]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "out.json"
            bench.write_json_report(path, meta, results)
            data = json.loads(path.read_text())
            self.assertEqual(data["meta"]["runs_per_model"], 3)
            self.assertEqual(len(data["models"]), 2)

    def test_markdown_sorted_by_score(self):
        import tempfile
        from pathlib import Path
        meta = {"runs_per_model": 3, "transcript_chars": 15234, "started_at": "2026-04-18T16:30:00Z"}
        results = [self._result("low", 50), self._result("high", 90), self._result("mid", 70)]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "out.md"
            bench.write_markdown_report(path, meta, results)
            txt = path.read_text()
            # Порядок строк: high, mid, low
            i_high = txt.index("| high ")
            i_mid = txt.index("| mid ")
            i_low = txt.index("| low ")
            self.assertLess(i_high, i_mid)
            self.assertLess(i_mid, i_low)

    def test_markdown_warnings_section(self):
        import tempfile
        from pathlib import Path
        meta = {"runs_per_model": 3, "transcript_chars": 100, "started_at": "2026-04-18T16:30:00Z"}
        r = self._result("x", 80)
        r["warnings"] = ["unload timeout"]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "out.md"
            bench.write_markdown_report(path, meta, [r])
            txt = path.read_text()
            self.assertIn("Предупреждения", txt)
            self.assertIn("unload timeout", txt)
```

- [ ] **Step 2: Запустить — упадут**

- [ ] **Step 3: Реализовать**

Добавить новую секцию:

```python
# === SECTION: REPORTS ===

def write_json_report(path: Path, meta: dict, results: list[dict]) -> None:
    payload = {"meta": meta, "models": results}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))


def _fmt(v, fmt="{:.1f}", default="—"):
    if v is None:
        return default
    try:
        return fmt.format(v)
    except (ValueError, TypeError):
        return str(v)


def write_markdown_report(path: Path, meta: dict, results: list[dict]) -> None:
    sorted_results = sorted(
        results,
        key=lambda r: r.get("aggregated", {}).get("quality_score_median") or -1,
        reverse=True,
    )
    lines = []
    lines.append("# Benchmark Results")
    lines.append("")
    lines.append(
        f"Транскрипт: {meta.get('transcript_chars', '?')} симв. · "
        f"Прогонов на модель: {meta.get('runs_per_model', '?')} · "
        f"{meta.get('started_at', '')}"
    )
    lines.append("")
    lines.append("| Модель | Status | Score | JSON | Cnt | Inference, с | Tok/s | VRAM, МБ | Size, МБ | Keyw | Spec |")
    lines.append("|---|:---:|---:|:---:|:---:|---:|---:|---:|---:|---:|---:|")
    for r in sorted_results:
        a = r.get("aggregated", {}) or {}
        status = r.get("status", "?")
        score = a.get("quality_score_median")
        json_ok = "✔" if a.get("json_valid_all_runs") else ("~" if a.get("json_valid_majority") else "✘")
        # Берём count_in_range из первого run-а как репрезентативный
        cnt = "?"
        if r.get("runs"):
            cnt = r["runs"][0].get("metrics", {}).get("summary_count", "?")
        lines.append(
            f"| {r['model']} | {status} | {_fmt(score, '{:.1f}')} | {json_ok} | {cnt} | "
            f"{_fmt(a.get('inference_s_median'), '{:.1f}')} | "
            f"{_fmt(a.get('tokens_per_sec_median'), '{:.0f}')} | "
            f"{_fmt(a.get('vram_peak_mb_median'), '{:.0f}')} | "
            f"{_fmt(a.get('size_total_mb_median'), '{:.0f}')} | "
            f"{_fmt(a.get('keyword_coverage_median'), '{:.2f}')} | "
            f"{_fmt(a.get('specificity_ratio_median'), '{:.2f}')} |"
        )

    # Warnings
    warn_blocks = [(r["model"], r.get("warnings", [])) for r in results if r.get("warnings")]
    if warn_blocks:
        lines.append("")
        lines.append("## Предупреждения")
        lines.append("")
        for model, ws in warn_blocks:
            for w in ws:
                lines.append(f"- `{model}`: {w}")

    # Сводка
    usable = [r for r in sorted_results
              if r.get("status") == "OK"
              and (r.get("aggregated", {}).get("quality_score_median") or 0) >= 70]
    lines.append("")
    lines.append("## Сводка")
    lines.append("")
    if sorted_results:
        top = sorted_results[0]
        lines.append(f"**Лучшая по качеству:** `{top['model']}` "
                     f"(score={_fmt(top.get('aggregated', {}).get('quality_score_median'), '{:.1f}')})")
    if usable:
        fastest = min(usable, key=lambda r: r["aggregated"].get("inference_s_median") or 1e9)
        lines.append(f"**Самая быстрая при score ≥ 70:** `{fastest['model']}` "
                     f"(inf={_fmt(fastest['aggregated'].get('inference_s_median'), '{:.1f}')}с)")
        leanest = min(usable, key=lambda r: r["aggregated"].get("vram_peak_mb_median") or 1e12)
        lines.append(f"**Самая экономная по VRAM при score ≥ 70:** `{leanest['model']}` "
                     f"(vram={_fmt(leanest['aggregated'].get('vram_peak_mb_median'), '{:.0f}')}МБ)")
    else:
        lines.append("**Нет моделей со score ≥ 70.**")
    lines.append("")
    lines.append("> Колонка `Judge` (LLM-as-judge от 0 до 10) добавляется оркестратором отдельно "
                 "после прогона — он читает `raw_response` каждой модели из `benchmark-results.json`.")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
```

- [ ] **Step 4: Запустить тесты — пройдут**

- [ ] **Step 5: Commit**

```bash
git add scripts/benchmark-ollama.py scripts/test_benchmark_ollama.py
git commit -m "Add JSON and Markdown report writers with summary section"
```

---

## Task 14: Main entry, argparse и end-to-end smoke

**Files:**
- Modify: `scripts/benchmark-ollama.py`

- [ ] **Step 1: Реализовать main**

Заменить заглушку `main()` на:

```python
# === SECTION: MAIN ===

def _load_models_from_file(path: Path) -> list[str]:
    """Читает список моделей из текстового файла (одна модель на строку, # — комментарий)."""
    out = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def _detect_gpu() -> dict:
    info = {"gpu": None, "gpu_total_vram_mb": None}
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0 and r.stdout.strip():
            first = r.stdout.strip().splitlines()[0]
            name, total = [x.strip() for x in first.split(",")]
            info["gpu"] = name
            info["gpu_total_vram_mb"] = int(total)
    except (FileNotFoundError, subprocess.SubprocessError, ValueError):
        pass
    return info


def main() -> int:
    ap = argparse.ArgumentParser(description="Benchmark Ollama models for yt-summary.")
    ap.add_argument("--transcript", default=str(DEFAULT_TRANSCRIPT))
    ap.add_argument("--title", default="Seedance 2.0 + Claude Code Creates $10k Websites in Minutes")
    ap.add_argument("--runs", type=int, default=RUNS_PER_MODEL)
    ap.add_argument("--url", default=os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    ap.add_argument("--models-file", type=str, default=None,
                    help="Файл со списком моделей (одна на строку). Иначе DEFAULT_MODELS из скрипта.")
    ap.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    ap.add_argument("--quiet", action="store_true",
                    help="Отключить TTY-режим прогресса (одна строка на событие).")
    args = ap.parse_args()

    transcript_path = Path(args.transcript)
    output_dir = Path(args.output_dir)
    per_model_dir = output_dir / "per-model"

    # Подготовка данных
    raw_content, lang = load_transcript(transcript_path)
    trimmed = raw_content[:TRIM_LIMIT]
    user_prompt = build_user_prompt(args.title, trimmed)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    # Список моделей
    if args.models_file:
        models = _load_models_from_file(Path(args.models_file))
    else:
        models = list(DEFAULT_MODELS)
    if not models:
        print("Список моделей пуст.", file=sys.stderr)
        return 2

    reporter = ProgressReporter(is_tty=False if args.quiet else None)
    reporter.overall_start(total_models=len(models), runs_per_model=args.runs)

    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    t_start = time.monotonic()
    results: list[dict] = []
    try:
        for i, model in enumerate(models, 1):
            try:
                r = run_model(
                    args.url, model, i, len(models),
                    messages, args.runs, KEYWORDS, reporter, per_model_dir,
                )
            except Exception as e:  # хотим продолжить со следующей моделью
                r = _skipped(model, f"unexpected error: {type(e).__name__}: {e}")
                reporter.model_skipped(str(e))
            results.append(r)
            # Промежуточный отчёт после каждой модели
            _write_reports(output_dir, started_at, args, transcript_path,
                           len(trimmed), results, t_start, finished=False)
    except KeyboardInterrupt:
        print("\n[BENCH] прервано пользователем — пишу частичный отчёт", file=sys.stderr)

    elapsed = time.monotonic() - t_start
    final_md = _write_reports(output_dir, started_at, args, transcript_path,
                              len(trimmed), results, t_start, finished=True)

    counts = {"OK": 0, "SKIPPED": 0, "UNUSABLE": 0}
    for r in results:
        counts[r.get("status", "SKIPPED")] = counts.get(r.get("status", "SKIPPED"), 0) + 1
    reporter.overall_done(
        elapsed=elapsed,
        ok=counts.get("OK", 0),
        skipped=counts.get("SKIPPED", 0),
        unusable=counts.get("UNUSABLE", 0),
        report_path=str(final_md),
    )
    return 0


def _write_reports(output_dir: Path, started_at: str, args, transcript_path: Path,
                   transcript_chars: int, results: list[dict],
                   t_start: float, finished: bool) -> Path:
    meta = {
        "transcript_path": str(transcript_path),
        "transcript_chars": transcript_chars,
        "trim_limit": TRIM_LIMIT,
        "runs_per_model": args.runs,
        "ollama_url": args.url,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds") if finished else None,
        "elapsed_s": round(time.monotonic() - t_start, 1),
        "host": _detect_gpu(),
        "partial": not finished,
    }
    json_path = output_dir / "benchmark-results.json"
    md_path = output_dir / "benchmark-results.md"
    write_json_report(json_path, meta, results)
    write_markdown_report(md_path, meta, results)
    return md_path
```

- [ ] **Step 2: Запустить unit-тесты — все PASS**

```bash
python3 -m unittest scripts.test_benchmark_ollama -v
```

- [ ] **Step 3: Smoke-тест на одной маленькой модели**

Создать временный файл с одной моделью, чтобы не качать весь список:

```bash
echo "llama3.2:3b" > /tmp/bench-smoke.txt
python3 scripts/benchmark-ollama.py \
  --models-file /tmp/bench-smoke.txt \
  --runs 1 \
  --output-dir /tmp/bench-smoke-out
```

Expected:
- В stderr виден прогресс по фазам (pulling, cleanup, warm-up, run 1/1, unload).
- В `/tmp/bench-smoke-out/benchmark-results.json` — мета + один объект модели с `runs[0].raw_response`.
- В `/tmp/bench-smoke-out/benchmark-results.md` — таблица с одной строкой и секция «Сводка».
- В `/tmp/bench-smoke-out/per-model/llama3.2_3b.json` — дубль данных модели.
- Exit code 0.

Если smoke падает — починить, но НЕ переписывать тесты. Smoke ловит реальные косяки HTTP/threading.

- [ ] **Step 4: Сделать скрипт исполняемым**

```bash
chmod +x scripts/benchmark-ollama.py
```

- [ ] **Step 5: Commit**

```bash
git add scripts/benchmark-ollama.py
git commit -m "Add main entry: argparse, models loop, intermediate reports, smoke-tested"
```

---

## Task 15: Финальная проверка — Ctrl+C и устойчивость к падениям

**Files:** none (only manual checks)

- [ ] **Step 1: Проверить, что Ctrl+C не теряет данные**

Запустить smoke-бенчмарк на 2 моделях, на 2-й нажать Ctrl+C во время прогона:

```bash
echo -e "llama3.2:3b\nllama3.2:1b" > /tmp/bench-ctrl-c.txt
python3 scripts/benchmark-ollama.py \
  --models-file /tmp/bench-ctrl-c.txt \
  --runs 1 \
  --output-dir /tmp/bench-ctrl-c-out
# Дождаться окончания первой модели, затем Ctrl+C на второй
```

Expected:
- `/tmp/bench-ctrl-c-out/per-model/llama3.2_3b.json` существует.
- `/tmp/bench-ctrl-c-out/benchmark-results.json` содержит первую модель и `meta.partial=true`.
- В stderr видно сообщение о прерывании.

- [ ] **Step 2: Проверить устойчивость к ошибочному имени модели**

```bash
echo "this-model-definitely-does-not-exist:99b" > /tmp/bench-bad.txt
python3 scripts/benchmark-ollama.py --models-file /tmp/bench-bad.txt --runs 1 \
  --output-dir /tmp/bench-bad-out
```

Expected: модель помечена `SKIPPED` с причиной из `pull failed: …`, exit code 0, отчёты сгенерированы.

- [ ] **Step 3: Финальный коммит (если smoke вскрыл правки)**

Если на шагах 1-2 что-то поправлено:

```bash
git add scripts/benchmark-ollama.py
git commit -m "Harden benchmark against Ctrl+C and missing models"
```

Иначе — пропустить.

---

## Self-Review Checklist (перед сдачей плана)

Запускается мной (автором плана) после написания. Найденные проблемы — фиксил inline.

**Spec coverage** (по `benchmark-plan.md`):
- ✅ Список моделей из плана → DEFAULT_MODELS (Task 1)
- ✅ Переиспользование промпта/схемы/options из test-ollama.py → Task 1
- ✅ `format=schema` (JSON_SCHEMA в payload) → Task 6 + Task 11
- ✅ TRIM_LIMIT=40000 → Task 1
- ✅ KEYWORDS из плана → Task 1
- ✅ CLI: --transcript, --runs, --url, --models-file, --output-dir, --quiet → Task 14
- ✅ `--skip-unload-check` — НЕ реализован (опущен как избыточный, можно добавить если понадобится)
- ✅ Прогресс-индикатор: TTY+не-TTY, фоновый таймер, формат строк → Task 10
- ✅ Алгоритм: pre-check → warm-up → 3 runs → unload → next → Task 11
- ✅ Промежуточное сохранение per-model + общий JSON/MD после каждой модели → Task 11 + Task 14
- ✅ Метрики качества (все из таблицы) → Task 3+4+5
- ✅ Метрики ресурсов (load/inference/total/tps/size/vram) → Task 11 (_do_one_run)
- ✅ nvidia-smi с graceful fallback → Task 9
- ✅ Unload через keep_alive=0 + поллинг /api/ps → Task 8
- ✅ JSON отчёт + Markdown с сортировкой и сводкой → Task 13
- ✅ Auto pull перед каждой моделью → Task 7 + Task 11
- ✅ LLM-as-judge — НЕ в скрипте (делается оркестратором), `raw_response` сохраняется в каждом run → Task 11

**Placeholder scan:** ни одного TBD/TODO/«implement later» в коде — все шаги содержат полный код.

**Type consistency:**
- `compute_metrics` возвращает dict с ключами `quality_score`, `keyword_coverage`, `specificity_ratio` и т.д. — те же ключи используются в `aggregate_runs` и `write_markdown_report`.
- `run_model` возвращает dict с ключами `model`, `status`, `runs`, `aggregated`, `warnings` — те же ключи читает `_write_reports` и `write_markdown_report`.
- `aggregate_runs` ключи `*_median`, `json_valid_all_runs`, `json_valid_majority` — все используются в writer'е.

**Гэп найден и исправлен:** в раннем драфте `aggregate_runs` не возвращал `size_total_mb_median`, но writer его читает. Добавил в `perf_keys`.


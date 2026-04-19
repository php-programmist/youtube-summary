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
    второй ключ при стандартным json.loads перезаписывает первый — теряется data).
    """
    return raw.count('"summary"')


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


def main() -> int:
    print("benchmark-ollama: skeleton OK", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

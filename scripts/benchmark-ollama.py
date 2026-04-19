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


def main() -> int:
    print("benchmark-ollama: skeleton OK", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

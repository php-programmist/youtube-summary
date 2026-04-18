#!/usr/bin/env python3
"""
Изолированный тест связки промпт + Ollama. Не трогает supadata.

Скрипт строит ровно такой же payload, что и нода
`Code - Build Ollama Request` в workflows/yt-summary-hourly.json.
ВАЖНО: при изменении промпта в воркфлоу — обнови и здесь (и наоборот).

Запуск:
  python3 scripts/test-ollama.py
  python3 scripts/test-ollama.py --transcript path/to/transcript.json --title "Своё название"
  python3 scripts/test-ollama.py --no-schema   # отключить JSON Schema, оставить просто format=json

Требует проброшенного порта 11434 (см. docker-compose.yml).
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRANSCRIPT = ROOT / "transcript.json"
TRIM_LIMIT = 40000

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


def build_user_prompt(title: str, content: str) -> str:
    return (
        f"Название: {title}\n\n"
        f"Субтитры:\n{content}\n\n"
        "Напоминание: верни JSON с массивом summary из 10-15 подробных пунктов "
        "(по 100-200 символов), с конкретными названиями инструментов, шагами и цифрами."
    )


def load_transcript(path: Path):
    data = json.loads(path.read_text())
    if isinstance(data, list):
        data = data[0]
    body = data.get("body", data)
    return body.get("content", ""), body.get("lang")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcript", default=str(TRANSCRIPT))
    ap.add_argument("--title", default="Seedance 2.0 + Claude Code Creates $10k Websites in Minutes")
    ap.add_argument("--model", default=os.environ.get("OLLAMA_MODEL", "qwen2.5:14b-instruct-q4_K_M"))
    ap.add_argument("--url", default=os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    ap.add_argument("--no-schema", action="store_true", help="Отключить JSON Schema (для сравнения)")
    args = ap.parse_args()

    raw_content, lang = load_transcript(Path(args.transcript))
    trimmed = raw_content[:TRIM_LIMIT]
    user_prompt = build_user_prompt(args.title, trimmed)

    fmt = "json" if args.no_schema else JSON_SCHEMA

    payload = {
        "model": args.model,
        "stream": False,
        "format": fmt,
        "keep_alive": os.environ.get("OLLAMA_KEEP_ALIVE_ACTIVE", "5m"),
        "options": {
            "temperature": 0.3,
            "num_ctx": 32768,
            "num_predict": 2000,
            "repeat_penalty": 1.3,
        },
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    }

    print(f"→ model={args.model}  url={args.url}  lang={lang}", file=sys.stderr)
    print(f"→ system prompt: {len(SYSTEM_PROMPT)} chars", file=sys.stderr)
    print(f"→ trimmed transcript: {len(trimmed)} / {len(raw_content)} chars", file=sys.stderr)
    print(f"→ format: {'JSON Schema (forced)' if not args.no_schema else 'json (loose)'}", file=sys.stderr)
    print(f"→ options: {payload['options']}", file=sys.stderr)
    print("→ запрос отправлен, ждём ответ...", file=sys.stderr)

    req = urllib.request.Request(
        f"{args.url}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    dt = time.time() - t0

    raw = body.get("message", {}).get("content", "")
    print(f"\n=== RAW OLLAMA RESPONSE ({dt:.1f}s) ===\n{raw}\n", file=sys.stderr)

    dup_count = raw.count('"summary"')
    if dup_count > 1:
        print(f"!!! ВНИМАНИЕ: ключ \"summary\" встречается {dup_count} раз — модель снова дробит массив", file=sys.stderr)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"!!! JSON parse failed: {e}", file=sys.stderr)
        sys.exit(1)

    summary = parsed.get("summary", [])
    main_idea = parsed.get("main_idea", "")
    print("=== PARSED ===")
    print(f"main_idea: {main_idea}")
    print(f"summary count: {len(summary)}")
    for i, s in enumerate(summary, 1):
        print(f"  {i:2d}. [{len(s):3d}ch] {s}")

    short = [s for s in summary if len(s) < 100]
    long = [s for s in summary if len(s) > 200]
    print(f"\nchecks: count_in_range={10 <= len(summary) <= 15}  too_short={len(short)}  too_long={len(long)}")


if __name__ == "__main__":
    main()

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


# === SECTION: PROGRESS ===

def _fmt_mmss(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60:02d}:{s % 60:02d}"


class ProgressReporter:
    """Прогресс-индикатор в stderr.

    TTY-режим: фоновый таймер обновляет текущую строку через \\r каждые ~2с.
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
        if self.is_tty:
            line = f"  {self._current_phase} · {_fmt_mmss(elapsed)}"
            self._write_inline(line)
            self._write("")  # перевод строки
        else:
            line = f"  {self._current_phase} · {elapsed:.1f}s"
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


def _skipped(model: str, reason: str) -> dict:
    return {
        "model": model,
        "status": "SKIPPED",
        "runs": [],
        "aggregated": {},
        "warnings": [reason],
    }


def main() -> int:
    print("benchmark-ollama: skeleton OK", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

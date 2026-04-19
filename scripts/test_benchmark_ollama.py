"""Unit-тесты для чистых функций scripts/benchmark-ollama.py.

Запуск:
  python3 -m unittest scripts.test_benchmark_ollama -v
  или
  cd scripts && python3 -m unittest test_benchmark_ollama -v
"""
import importlib.util
import json
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


if __name__ == "__main__":
    unittest.main()

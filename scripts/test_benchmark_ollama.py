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


if __name__ == "__main__":
    unittest.main()

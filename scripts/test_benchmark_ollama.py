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


if __name__ == "__main__":
    unittest.main()

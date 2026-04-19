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


if __name__ == "__main__":
    unittest.main()

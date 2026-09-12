import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from fence.cache import Cache


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.counts = (Counter({"aaa": 2, "bbb": 1}), Counter({"ccc": 3}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_counts_survive_a_round_trip(self):
        cache = Cache(self.root, 3)
        cache.put("src/app.py", "source text", self.counts)
        cache.save()
        self.assertEqual(Cache(self.root, 3).get("src/app.py", "source text"), self.counts)

    def test_changed_content_is_a_miss(self):
        cache = Cache(self.root, 3)
        cache.put("src/app.py", "source text", self.counts)
        self.assertIsNone(cache.get("src/app.py", "source text, edited"))

    def test_a_different_fingerprint_scheme_is_discarded(self):
        cache = Cache(self.root, 3)
        cache.put("src/app.py", "source text", self.counts)
        cache.save()
        self.assertIsNone(Cache(self.root, 4).get("src/app.py", "source text"))

    def test_a_corrupt_cache_is_ignored_rather_than_fatal(self):
        (self.root / ".fence").mkdir()
        (self.root / ".fence" / "cache.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(Cache(self.root, 3).get("src/app.py", "source text"))

    def test_files_that_are_gone_are_dropped(self):
        cache = Cache(self.root, 3)
        cache.put("src/app.py", "source text", self.counts)
        cache.put("src/gone.py", "source text", self.counts)
        cache.save(keep={"src/app.py"})
        stored = json.loads((self.root / ".fence" / "cache.json").read_text())
        self.assertEqual(list(stored["files"]), ["src/app.py"])

    def test_it_keeps_itself_out_of_commits(self):
        cache = Cache(self.root, 3)
        cache.put("src/app.py", "source text", self.counts)
        cache.save()
        self.assertIn("cache.json", (self.root / ".fence" / ".gitignore").read_text())


if __name__ == "__main__":
    unittest.main()

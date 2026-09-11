"""Run the language-neutral cases in conformance/cases/*.toml.

These are the contract between implementations rather than tests of this one:
any port must reach the same verdicts. See docs/FORMAT.md.
"""

import tomllib
import unittest
from pathlib import Path

from fence import anchor as A

CASES_DIR = Path(__file__).resolve().parents[2] / "conformance" / "cases"


class Files:
    """The reader interface, backed by a dict of path -> source."""

    def __init__(self, files):
        self.files = files

    def read(self, path):
        return self.files.get(path)

    def paths(self):
        return list(self.files)


def load_cases():
    for path in sorted(CASES_DIR.glob("*.toml")):
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        for case in data.get("case", []):
            yield path.stem, case


def anchor_for(case):
    """Pin the first statement in `before` whose first line contains anchor_match."""
    index = A.Index(Files(case["before"]))
    for path in case["before"]:
        for candidate in index.get(path):
            if case["anchor_match"] in candidate.snippet.splitlines()[0]:
                return A.make_anchor(
                    candidate, index.get(path),
                    index.copies_elsewhere(path, candidate.exact, candidate.shape))
    raise AssertionError(f"no statement matching {case['anchor_match']!r} in {case['name']!r}")


class ConformanceTest(unittest.TestCase):
    def test_every_case(self):
        cases = list(load_cases())
        self.assertTrue(cases, f"no conformance cases found in {CASES_DIR}")
        for group, case in cases:
            with self.subTest(f"{group}: {case['name']}"):
                match = A.locate(anchor_for(case), A.Index(Files(case["after"])))
                self.assertEqual(match.how, case["expect"])
                if "expect_file" in case:
                    self.assertEqual(match.candidate.path, case["expect_file"])
                if "expect_scope" in case:
                    self.assertEqual(match.candidate.scope, case["expect_scope"])


if __name__ == "__main__":
    unittest.main()

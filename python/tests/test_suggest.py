import unittest

from fence import anchor as A
from fence import suggest

SOURCE = '''\
import time


def focus(element):
    # Safari fires focus twice within 50ms, and the second one lands on the
    # wrong node, so the first is swallowed deliberately.
    time.sleep(0.05)
    element.focus()


def plain(a, b):
    return a + b


def described(values):
    # Sum the values
    return sum(values)
'''


def statement(source, text, path="app.py"):
    for candidate in A.candidates(path, source):
        if candidate.snippet.strip().splitlines()[0].strip().startswith(text):
            return candidate
    raise AssertionError(f"no statement starting {text!r}")


def rank(source, text, path="app.py", copies=0, neighbouring=False):
    candidates = A.candidates(path, source)
    candidate = statement(source, text, path)
    lines = source.split("\n")
    return suggest.rank(candidate, suggest.own_text(candidate, candidates, lines),
                        suggest.comment_for(lines, candidate.line), copies, neighbouring)


class CommentTest(unittest.TestCase):
    def test_it_collects_the_block_directly_above(self):
        lines = SOURCE.split("\n")
        line = statement(SOURCE, "time.sleep").line
        self.assertIn("Safari fires focus twice", suggest.comment_above(lines, line))
        self.assertIn("swallowed deliberately", suggest.comment_above(lines, line))

    def test_a_statement_with_nothing_above_it_has_no_comment(self):
        lines = SOURCE.split("\n")
        self.assertEqual(suggest.comment_above(lines, statement(SOURCE, "return a + b").line), "")

    def test_a_trailing_comment_counts_too(self):
        source = 'import time\n\n\ndef go():\n    time.sleep(0.05)  # safari fires focus twice\n'
        found = rank(source, "time.sleep")
        self.assertIn("safari", found.draft)


class RankTest(unittest.TestCase):
    def test_an_explaining_comment_is_the_strongest_signal(self):
        found = rank(SOURCE, "time.sleep")
        self.assertGreaterEqual(found.score, 5)
        self.assertIn("Safari", found.draft)
        self.assertIn("fence add app.py:", found.command)

    def test_a_comment_that_only_describes_is_worth_little(self):
        found = rank(SOURCE, "return sum(values)")
        self.assertLess(found.score, 5)
        self.assertIsNone(found.draft)

    def test_a_statement_with_no_signal_is_not_suggested(self):
        self.assertLessEqual(rank(SOURCE, "return a + b").score, 0)

    def test_without_a_draft_it_offers_to_borrow_the_commit_message(self):
        found = rank(SOURCE, "return sum(values)")
        self.assertIn("--from-blame", found.command)

    def test_a_statement_repeated_everywhere_is_a_convention(self):
        """The discount that keeps the list honest, borrowed from asof."""
        alone = rank(SOURCE, "time.sleep", copies=0)
        common = rank(SOURCE, "time.sleep", copies=6)
        self.assertEqual(common.score, alone.score - 4)
        self.assertIn("convention rather than a decision", " ".join(common.reasons))

    def test_a_reason_recorded_next_door_is_discounted(self):
        """Found by running suggest on this repository: it proposed the statement
        wrapped around one that already had a note, for the same reason."""
        alone = rank(SOURCE, "time.sleep")
        already = rank(SOURCE, "time.sleep", neighbouring=True)
        self.assertEqual(already.score, alone.score - 3)
        self.assertIn("already recorded", " ".join(already.reasons))

    def test_a_test_file_is_discounted(self):
        here = rank(SOURCE, "time.sleep", path="tests/test_app.py")
        there = rank(SOURCE, "time.sleep", path="app.py")
        self.assertEqual(here.score, there.score - 2)

    def test_a_signal_inside_a_nested_statement_belongs_to_that_statement(self):
        """Found by running it on real code: `def run` outranked the sleep inside it."""
        self.assertLessEqual(rank(SOURCE, "def focus").score, 0)
        self.assertGreater(rank(SOURCE, "time.sleep").score, 0)

    def test_a_signal_word_inside_a_string_is_not_the_code_speaking(self):
        source = 'def read(path):\n    return path.read_text(encoding="utf-8", errors="replace")\n'
        self.assertLessEqual(rank(source, "return path.read_text").score, 0)

    def test_a_structural_number_is_not_a_chosen_one(self):
        """Found by running it on this repository: a dataclass default of 1.0."""
        self.assertFalse(suggest.chosen_number("similarity: float = 1.0"))
        self.assertFalse(suggest.chosen_number("index = 0"))
        self.assertTrue(suggest.chosen_number("time.sleep(0.05)"))
        self.assertTrue(suggest.chosen_number("PORT = 8420"))

    def test_timing_and_a_chosen_number_both_count(self):
        found = rank(SOURCE, "time.sleep")
        signals = " ".join(found.reasons)
        self.assertIn("timing primitive", signals)
        self.assertIn("number somebody chose", signals)


if __name__ == "__main__":
    unittest.main()

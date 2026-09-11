import unittest

from fence import anchor as A

BASE = '''\
import time


class Client:
    def fetch(self, url):
        resp = self.session.get(url, timeout=10)
        if resp.json().get("error") == "unauthorized":
            raise AuthError(url)
        return resp


def focus(el):
    time.sleep(0.05)
    el.focus()
'''

GUARD = 'if resp.json().get("error") == "unauthorized":'
GUARD_BLOCK = f"        {GUARD}\n            raise AuthError(url)\n"
HELPER = '''

def _check(resp, url):
    if resp.json().get("error") == "unauthorized":
        raise AuthError(url)
'''


class DictReader:
    def __init__(self, files):
        self.files = files

    def read(self, path):
        return self.files.get(path)

    def paths(self):
        return list(self.files)


def line_of(source, text):
    for number, line in enumerate(source.split("\n"), 1):
        if text in line:
            return number
    raise AssertionError(f"{text!r} not in source")


def anchor_for(source, text, path="app.py"):
    cands = A.candidates(path, source)
    line = line_of(source, text)
    return A.make_anchor(A.pick(cands, line, line), cands)


def relocate(anchor, files):
    return A.locate(anchor, A.Index(DictReader(files)))


class PickTest(unittest.TestCase):
    def test_picks_the_statement_that_starts_on_the_line(self):
        c = A.pick(A.candidates("app.py", BASE), line_of(BASE, GUARD), line_of(BASE, GUARD))
        self.assertEqual((c.kind, c.scope), ("If", "Client.fetch"))
        self.assertEqual(c.end_line, line_of(BASE, "raise AuthError"))

    def test_a_range_picks_the_smallest_statement_covering_it(self):
        c = A.pick(A.candidates("app.py", BASE), line_of(BASE, "resp = "), line_of(BASE, "return resp"))
        self.assertEqual((c.kind, c.scope), ("FunctionDef", "Client"))

    def test_decorator_lines_belong_to_the_function(self):
        src = "@retry(3)\ndef f():\n    pass\n"
        c = A.pick(A.candidates("app.py", src), 1, 1)
        self.assertEqual((c.kind, c.line), ("FunctionDef", 1))


class RelocateTest(unittest.TestCase):
    def setUp(self):
        self.anchor = anchor_for(BASE, GUARD)

    def test_unchanged_code_is_ok(self):
        self.assertEqual(relocate(self.anchor, {"app.py": BASE}).how, "ok")

    def test_reformatting_comments_and_shifted_lines_are_ignored(self):
        src = "import os\n" + BASE.replace(
            GUARD, 'if resp.json().get(\n            "error"\n        ) == "unauthorized":  # vendor quirk')
        m = relocate(self.anchor, {"app.py": src})
        self.assertEqual(m.how, "ok")
        self.assertEqual(m.candidate.line, line_of(src, "if resp.json().get("))

    def test_consistent_rename_is_followed(self):
        m = relocate(self.anchor, {"app.py": BASE.replace("resp", "response")})
        self.assertEqual(m.how, "renamed")

    def test_move_to_another_function_is_followed(self):
        src = BASE.replace(GUARD_BLOCK, "        self._check(resp, url)\n") + HELPER
        m = relocate(self.anchor, {"app.py": src})
        self.assertEqual((m.how, m.candidate.scope), ("moved", "_check"))

    def test_move_to_another_file_is_followed(self):
        src = BASE.replace(GUARD_BLOCK, "        _check(resp, url)\n")
        m = relocate(self.anchor, {"app.py": src, "auth.py": HELPER})
        self.assertEqual((m.how, m.candidate.path), ("moved", "auth.py"))

    def test_edit_in_place_is_changed(self):
        m = relocate(self.anchor, {"app.py": BASE.replace('"unauthorized"', '"forbidden"')})
        self.assertEqual(m.how, "changed")
        self.assertTrue(A.CHANGED_THRESHOLD <= m.similarity < 1)

    def test_deleted_code_is_removed(self):
        m = relocate(self.anchor, {"app.py": BASE.replace(GUARD_BLOCK, "")})
        self.assertEqual(m.how, "removed")

    def test_deleted_file_is_removed(self):
        self.assertEqual(relocate(self.anchor, {}).how, "removed")

    def test_unparseable_file_is_skipped_not_removed(self):
        m = relocate(self.anchor, {"app.py": BASE + "\ndef broken(:\n"})
        self.assertEqual(m.how, "unparseable")


class LookalikeTest(unittest.TestCase):
    """Short, generic statements must not be 'found' in some other lookalike."""

    def test_a_lookalike_guard_does_not_stand_in_for_a_deleted_one(self):
        src = "def load(a, b):\n    if a is None:\n        return\n    if b is None:\n        return\n    return a + b\n"
        anchor = anchor_for(src, "if a is None")
        m = relocate(anchor, {"app.py": src.replace("    if a is None:\n        return\n", "")})
        self.assertEqual(m.how, "removed")

    def test_a_generic_statement_is_not_followed_into_another_function(self):
        src = "def a():\n    return None\n\n\ndef b():\n    return None\n"
        anchor = anchor_for(src, "return None")
        m = relocate(anchor, {"app.py": "def a():\n    pass\n\n\ndef b():\n    return None\n"})
        self.assertEqual(m.how, "removed")

    def test_a_small_constant_change_is_changed_not_removed(self):
        anchor = anchor_for(BASE, "time.sleep")
        m = relocate(anchor, {"app.py": BASE.replace("0.05", "0.1")})
        self.assertEqual(m.how, "changed")

    def test_removing_one_of_two_identical_statements_is_ambiguous(self):
        src = "def sync(client):\n    client.retry()\n    client.retry()\n"
        anchor = anchor_for(src, "client.retry()")
        m = relocate(anchor, {"app.py": "def sync(client):\n    client.retry()\n"})
        self.assertEqual(m.how, "ambiguous")


if __name__ == "__main__":
    unittest.main()

"""End to end: a real git repo, the real pre-commit hook, the real CLI."""

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from fence.cli import main
from fence.store import Store
from tests.test_anchor import BASE, GUARD_BLOCK, line_of

FIRST_COMMIT = "Check the body for auth errors\n\nThe vendor API returns 200 even when auth fails."
REASON = "Vendor API returns 200 on auth failure; the error is in the body."


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.repo = Path(self.tmp.name)
        self.old_cwd = os.getcwd()
        os.chdir(self.repo)
        git(self.repo, "init", "-q")
        for key, value in (("user.name", "Test"), ("user.email", "test@example.com"),
                           ("core.autocrlf", "false"), ("commit.gpgsign", "false")):
            git(self.repo, "config", key, value)
        (self.repo / "app.py").write_text(BASE, newline="\n")
        git(self.repo, "add", "app.py")
        git(self.repo, "commit", "-q", "-m", FIRST_COMMIT)

        self.assertEqual(self.fence("init")[0], 0)
        code, out = self.fence("add", f"app.py:{line_of(BASE, 'if resp.json()')}", "-m", REASON)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.commit("Add note").returncode, 0)
        self.note = Store(self.repo).notes()[0]

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def fence(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(list(args))
        return code, out.getvalue()

    def commit(self, message="change"):
        git(self.repo, "add", "-A")
        return git(self.repo, "commit", "-q", "-m", message)

    def edit(self, old, new):
        path = self.repo / "app.py"
        text = path.read_text()
        self.assertIn(old, text)
        path.write_text(text.replace(old, new), newline="\n")

    def test_hook_blocks_a_commit_that_deletes_reasoned_code(self):
        self.edit(GUARD_BLOCK, "")
        result = self.commit()
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn("[REMOVED]", output)
        self.assertIn(REASON, output)

    def test_retiring_the_note_unblocks_the_commit_and_keeps_history(self):
        self.edit(GUARD_BLOCK, "")
        code, out = self.fence("retire", self.note["id"], "-m", "Vendor fixed their status codes")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.commit().returncode, 0)
        self.assertEqual(Store(self.repo).notes(), [])
        retired = json.loads((self.repo / ".fence" / "retired" / f"{self.note['id']}.json").read_text())
        self.assertEqual(retired["retired_because"], "Vendor fixed their status codes")

    def test_unrelated_edits_commit_quietly(self):
        self.edit("    el.focus()\n", "    el.focus()\n    el.scroll_into_view()\n")
        result = self.commit()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout + result.stderr, "")

    def test_rename_is_reported_then_followed_by_update(self):
        self.edit("resp", "response")
        code, out = self.fence("check")
        self.assertEqual(code, 0)
        self.assertIn("[renamed]", out)
        self.fence("update")
        code, out = self.fence("check")
        self.assertIn("[ok]", out)

    def test_confirm_accepts_a_change(self):
        self.edit('"unauthorized"', '"forbidden"')
        self.assertIn("[changed", self.fence("check")[1])
        self.assertEqual(self.fence("confirm", self.note["id"][:4])[0], 0)
        self.assertIn("[ok]", self.fence("check")[1])

    def test_add_from_blame_borrows_the_commit_message(self):
        code, out = self.fence("add", f"app.py:{line_of(BASE, 'time.sleep')}", "--from-blame")
        self.assertEqual(code, 0, out)
        note = next(n for n in Store(self.repo).notes() if n["id"] != self.note["id"])
        self.assertEqual(note["reason"], FIRST_COMMIT)
        self.assertTrue(note["source"].startswith("commit "))

    def test_the_parse_cache_is_written_and_stays_out_of_the_commit(self):
        code, out = self.fence("add", f"app.py:{line_of(BASE, 'time.sleep')}",
                               "-m", "Safari fires focus twice without it")
        self.assertEqual(code, 0, out)
        cache = self.repo / ".fence" / "cache.json"
        self.assertTrue(cache.is_file())
        self.assertIn("app.py", json.loads(cache.read_text(encoding="utf-8"))["files"])
        self.assertEqual(self.commit("second note").returncode, 0)
        self.assertNotIn("cache.json", git(self.repo, "ls-files").stdout)

    def test_a_warm_cache_gives_the_same_answers(self):
        self.fence("add", f"app.py:{line_of(BASE, 'time.sleep')}", "-m", "keep the delay")
        cold = self.fence("check")[1]
        warm = self.fence("check")[1]
        self.assertEqual(cold.count("[ok]"), 2)
        self.assertEqual(cold, warm)

    def test_an_edit_is_noticed_even_though_the_cache_is_warm(self):
        self.fence("check")
        self.edit(GUARD_BLOCK, "")
        code, out = self.fence("check")
        self.assertEqual(code, 1)
        self.assertIn("[REMOVED]", out)

    def test_bad_location_is_a_clear_error(self):
        code, out = self.fence("add", "app.py", "-m", "x")
        self.assertEqual(code, 2)
        self.assertIn("expected <file>:<line>", out)


if __name__ == "__main__":
    unittest.main()

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

    def test_a_second_note_on_the_same_statement_is_refused(self):
        guard = f"app.py:{line_of(BASE, 'if resp.json()')}"
        code, out = self.fence("add", guard, "-m", "the same statement again")
        self.assertEqual(code, 2)
        self.assertIn(self.note["id"], out)
        self.assertIn("--also", out)
        self.assertEqual(len(Store(self.repo).notes()), 1)

    def test_also_records_a_second_independent_reason(self):
        guard = f"app.py:{line_of(BASE, 'if resp.json()')}"
        code, out = self.fence("add", guard, "--also", "-m", "the proxy rewrites 401 to 200 too")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(Store(self.repo).notes()), 2)

    def test_another_statement_in_the_same_file_is_fine(self):
        code, out = self.fence("add", f"app.py:{line_of(BASE, 'time.sleep')}", "-m", "keep the delay")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(Store(self.repo).notes()), 2)

    def test_statements_lists_what_a_note_could_be_pinned_to(self):
        payload = json.loads(self.fence("statements", "app.py", "--json")[1])
        self.assertEqual(payload["path"], "app.py")
        guard = next(s for s in payload["statements"] if s["line"] == line_of(BASE, "if resp.json()"))
        self.assertEqual((guard["kind"], guard["scope"]), ("If", "Client.fetch"))
        self.assertGreater(guard["tokens"], 12)
        self.assertTrue(guard["exact"])

    def test_why_gives_the_reason_recorded_for_a_file(self):
        code, out = self.fence("why", "app.py")
        self.assertEqual(code, 0, out)
        self.assertIn(REASON, out)
        self.assertIn(self.note["id"], out)

    def test_why_narrows_to_a_line(self):
        covered = line_of(BASE, "if resp.json()")
        self.assertIn(REASON, self.fence("why", f"app.py:{covered}")[1])
        self.assertIn("nothing recorded", self.fence("why", f"app.py:{line_of(BASE, 'def focus')}")[1])

    def test_why_json_is_machine_readable(self):
        payload = json.loads(self.fence("why", "app.py", "--json")[1])
        found = payload["notes"][0]
        self.assertEqual(found["id"], self.note["id"])
        self.assertEqual(found["status"], "ok")
        self.assertEqual(found["reason"], REASON)
        self.assertEqual(found["found_at"]["path"], "app.py")

    def test_check_json_says_what_is_blocking(self):
        self.edit(GUARD_BLOCK, "")
        code, out = self.fence("check", "--json")
        payload = json.loads(out)
        self.assertEqual(code, 1)
        self.assertTrue(payload["blocked"])
        self.assertEqual(payload["counts"], {"removed": 1})
        self.assertEqual(payload["notes"][0]["status"], "removed")
        self.assertIsNone(payload["notes"][0]["found_at"])

    def test_init_agents_installs_instructions_once(self):
        self.fence("init", "--agents")
        agents = (self.repo / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("fence why", agents)
        skill = self.repo / ".claude" / "skills" / "fence" / "SKILL.md"
        self.assertIn("name: fence", skill.read_text(encoding="utf-8"))
        self.fence("init", "--agents")
        self.assertEqual((self.repo / "AGENTS.md").read_text(encoding="utf-8"), agents)

    def test_init_leaves_an_explanation_for_whoever_finds_the_directory(self):
        readme = self.repo / ".fence" / "README.md"
        self.assertTrue(readme.is_file())
        text = readme.read_text(encoding="utf-8")
        self.assertIn("fence retire", text)
        self.assertIn("git does not clone", text)
        self.assertIn(".fence/README.md", git(self.repo, "ls-files").stdout)

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

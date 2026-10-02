"""action/pr_comment.py: which notes a pull request touches, and the one comment about them."""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from fence.cli import main as fence
from tests.test_anchor import BASE, GUARD_BLOCK, line_of

SCRIPT = Path(__file__).resolve().parents[2] / "action" / "pr_comment.py"


def load():
    spec = importlib.util.spec_from_file_location("pr_comment", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(SCRIPT.is_file(), "the action is not alongside this copy")
class TestChangedLines(unittest.TestCase):
    def setUp(self):
        self.pc = load()

    def test_spans_files_and_renames(self):
        diff = "\n".join([
            "diff --git a/src/a.py b/src/a.py",
            "--- a/src/a.py",
            "+++ b/src/a.py",
            "@@ -10,2 +10,3 @@ def f():",
            "-    x = 1",
            "++ this text starts with two plus signs",
            "-- and this one with two minus signs",
            "@@ -40 +41,0 @@",
            "-    gone()",
            "diff --git a/old.py b/new.py",
            "similarity index 100%",
            "rename from old.py",
            "rename to new.py",
            "diff --git a/dead.py b/dead.py",
            "deleted file mode 100644",
            "--- a/dead.py",
            "+++ /dev/null",
            "@@ -1,3 +0,0 @@",
            "-a",
        ])
        touched = self.pc.changed_lines(diff)
        self.assertEqual(touched["src/a.py"], [(10, 12), (41, 42)])
        self.assertEqual(touched["old.py"], [])
        self.assertEqual(touched["new.py"], [])
        self.assertEqual(touched["dead.py"], [])
        self.assertNotIn("this text starts with two plus signs", touched)
        self.assertNotIn("and this one with two minus signs", touched)


def note(status, path="a.py", line=5, end=7, found=True, **extra):
    return dict({
        "id": f"id-{status}-{line}",
        "status": status,
        "reason": f"why {status}",
        "source": None,
        "similarity": 0.62 if status == "changed" else None,
        "recorded_at": {"path": path, "line": line, "scope": "f"},
        "found_at": {"path": path, "line": line, "end_line": end, "scope": "f"} if found else None,
    }, **extra)


@unittest.skipUnless(SCRIPT.is_file(), "the action is not alongside this copy")
class TestAffected(unittest.TestCase):
    def setUp(self):
        self.pc = load()

    def test_only_what_the_pull_request_touched(self):
        notes = [
            note("removed", found=False),
            note("changed", line=20, end=22),
            note("ok", line=30, end=31),             # its lines are in the diff
            note("ok", line=50, end=52),             # same file, lines untouched
            note("renamed", path="other.py"),        # file not in the diff at all
            note("foreign", line=60, end=61),        # another build's: not ours to judge
        ]
        touched = {"a.py": [(31, 31)]}
        picked = [n["id"] for n in self.pc.affected(notes, touched)]
        self.assertEqual(picked, ["id-removed-5", "id-changed-20", "id-ok-30"])

    def test_a_move_is_reported_from_either_end(self):
        moved = note("moved")
        moved["found_at"] = {"path": "b.py", "line": 3, "end_line": 4, "scope": "g"}
        self.assertEqual(len(self.pc.affected([moved], {"b.py": []})), 1)
        self.assertEqual(len(self.pc.affected([moved], {"a.py": []})), 1)


@unittest.skipUnless(SCRIPT.is_file(), "the action is not alongside this copy")
class TestRender(unittest.TestCase):
    def setUp(self):
        self.pc = load()

    def test_body(self):
        removed = note("removed", found=False, reason="Ask @alice before touching this.",
                       source="https://example.com/ticket/7")
        changed = note("changed", line=20, end=22)
        body = self.pc.render([removed, changed], "https://github.com/o/r/blob/abc")
        self.assertTrue(body.startswith(self.pc.MARKER))
        self.assertIn("**Removed** · was `a.py:5` in `f`", body)
        self.assertIn("**Changed (62% similar)** · [`a.py:20-22` in `f`]"
                      "(https://github.com/o/r/blob/abc/a.py#L20-L22)", body)
        self.assertIn("@​alice", body)                      # quoted, not a page
        self.assertIn("> — https://example.com/ticket/7", body)
        self.assertIn("fence retire", body)
        self.assertIn("fence confirm", body)

    def test_all_clear(self):
        body = self.pc.render([], None)
        self.assertIn("no longer touches", body)


class FakeGitHub(BaseHTTPRequestHandler):
    comments: list = []
    log: list = []

    def log_message(self, *args):
        pass

    def reply(self, payload, status=200):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        return json.loads(self.rfile.read(int(self.headers["Content-Length"])))

    def do_GET(self):
        self.log.append(("GET", self.path))
        self.reply(self.comments if "page=1" in self.path else [])

    def do_POST(self):
        self.log.append(("POST", self.path))
        comment = {"id": len(self.comments) + 1, "body": self.body()["body"]}
        self.comments.append(comment)
        self.reply(comment, 201)

    def do_PATCH(self):
        self.log.append(("PATCH", self.path))
        cid = int(self.path.rsplit("/", 1)[1])
        comment = next(c for c in self.comments if c["id"] == cid)
        comment["body"] = self.body()["body"]
        self.reply(comment)


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


@unittest.skipUnless(SCRIPT.is_file(), "the action is not alongside this copy")
class TestEndToEnd(unittest.TestCase):
    """A real repository, a real note, a pull request that deletes its statement."""

    def setUp(self):
        self.pc = load()
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.repo = Path(self.tmp.name)
        self.old_cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.chdir, self.old_cwd)
        git(self.repo, "init", "-q")
        for key, value in (("user.name", "T"), ("user.email", "t@example.com"),
                           ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
            git(self.repo, "config", key, value)
        (self.repo / "app.py").write_text(BASE, newline="\n")
        self.quiet("init")
        self.quiet("add", f"app.py:{line_of(BASE, 'if resp.json()')}",
                   "-m", "Vendor returns 200 on auth failure. Ask @alice.")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base", "--no-verify")
        self.base = git(self.repo, "rev-parse", "HEAD").stdout.strip()

        FakeGitHub.comments, FakeGitHub.log = [], []
        self.server = HTTPServer(("127.0.0.1", 0), FakeGitHub)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.env = {
            "CHECK_JSON": str(self.repo / ".git" / "check.json"),
            "BASE_SHA": self.base, "HEAD_SHA": "f00d", "PR_NUMBER": "7",
            "GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": "t",
            "GITHUB_API_URL": f"http://127.0.0.1:{self.server.server_port}",
        }

    def quiet(self, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return fence(list(args))

    def commit(self, text):
        (self.repo / "app.py").write_text(text, newline="\n")
        git(self.repo, "commit", "-q", "-am", "pr", "--no-verify")

    def run_action(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fence(["check", "--json"])
        Path(self.env["CHECK_JSON"]).write_text(out.getvalue(), encoding="utf-8")
        old = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        try:
            with contextlib.redirect_stdout(io.StringIO()) as printed:
                self.assertEqual(self.pc.main(), 0)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return printed.getvalue()

    def test_comment_lifecycle(self):
        # A pull request that leaves the guard alone says nothing at all.
        self.commit(BASE.replace("time.sleep", "time.sleep  # unrelated", 1))
        self.assertIn("comment nothing to say", self.run_action())
        self.assertEqual(FakeGitHub.comments, [])

        # One that deletes it gets one comment quoting the reason.
        self.commit(BASE.replace(GUARD_BLOCK, ""))
        self.assertIn("comment posted", self.run_action())
        [comment] = FakeGitHub.comments
        self.assertIn("**Removed**", comment["body"])
        self.assertIn("Vendor returns 200 on auth failure", comment["body"])
        self.assertIn("@​alice", comment["body"])

        # Pushing again with the same result leaves it alone...
        self.assertIn("comment unchanged", self.run_action())
        # ...and putting the guard back turns it into an all-clear, not a second comment.
        self.commit(BASE)
        self.assertIn("comment updated", self.run_action())
        self.assertEqual(len(FakeGitHub.comments), 1)
        self.assertIn("no longer touches", FakeGitHub.comments[0]["body"])
        self.assertEqual([m for m, _ in FakeGitHub.log].count("POST"), 1)

    def test_a_refused_token_warns_and_passes(self):
        self.commit(BASE.replace(GUARD_BLOCK, ""))
        self.server.shutdown()
        self.server.server_close()
        self.assertIn("::warning::fence could not comment", self.run_action())


if __name__ == "__main__":
    unittest.main()

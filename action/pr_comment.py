"""Quote the recorded reasons a pull request touches, in one comment on it.

The hook speaks up at commit time, on the machine of whoever is committing. A
reviewer never sees it, and neither does anyone merging from the web. This puts
the same information where the decision to merge is made.

Run by action.yml after `fence check --json` has written the notes to a file.
Standard library only, and either build's JSON: the two share one shape.

A note is reported when the pull request changes the file it lives in and the
note is not `ok` there -- removed, changed, moved, renamed, ambiguous -- or when
the diff edits lines inside the statement itself. One comment per pull request,
found again by a marker and rewritten on every push, so it never piles up. When
nothing is affected any more, an existing comment says so rather than lingering
as a stale warning; when nothing ever was, nothing is posted.

Posting is best effort. A pull request from a fork gets a read-only token, and a
missing comment must not fail a check that otherwise passed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

MARKER = "<!-- fence:pr-comment -->"
MAX_NOTES = 50
MAX_REASON = 1500

ORDER = ["removed", "changed", "ambiguous", "moved", "renamed", "ok"]
HEADINGS = {
    "removed": "Removed",
    "changed": "Changed",
    "ambiguous": "One copy of several removed",
    "moved": "Moved",
    "renamed": "Renamed",
    "ok": "Edited",
}


# ----------------------------------------------------------------------- the diff

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _path(header: str) -> str | None:
    """`+++ b/src/a.py` -> `src/a.py`; `/dev/null` -> None."""
    name = header[4:].rstrip("\t\n")
    if name == "/dev/null":
        return None
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape")
        name = name.encode("latin-1").decode("utf-8", "replace")
    return name[2:] if name[:2] in ("a/", "b/") else name


def changed_lines(diff: str) -> dict[str, list[tuple[int, int]]]:
    """Every path the diff touches, with the line spans it changed in the new file.

    A deleted or renamed-away file maps to no spans: being in the dict at all is
    what says the pull request touched it. A pure deletion inside a file marks
    the lines either side of where the text was, since that is where it was.
    """
    touched: dict[str, list[tuple[int, int]]] = {}
    current = None
    header = False  # inside a file's header, where --- and +++ are names, not text
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            header, current = True, None
        elif header and line.startswith("--- "):
            old = _path(line)
            if old is not None:
                touched.setdefault(old, [])
        elif header and line.startswith("+++ "):
            current = _path(line)
            if current is not None:
                touched.setdefault(current, [])
        elif header and (line.startswith("rename from ") or line.startswith("rename to ")):
            touched.setdefault(line.split(" ", 2)[2], [])
        elif line.startswith("@@"):
            header = False
            match = HUNK.match(line)
            if match and current is not None:
                start = int(match.group(1))
                count = int(match.group(2)) if match.group(2) is not None else 1
                span = (start, start + count - 1) if count else (start, start + 1)
                touched[current].append(span)
    return touched


def git_diff(base: str) -> str:
    def git(*args, check=True):
        return subprocess.run(["git", "-c", "core.quotePath=false", *args],
                              capture_output=True, text=True, check=check,
                              encoding="utf-8", errors="replace")

    # actions/checkout fetches one commit. The base is a tree away, not a history
    # away: one more shallow fetch is enough to diff against it.
    if git("cat-file", "-e", f"{base}^{{commit}}", check=False).returncode != 0:
        git("fetch", "--no-tags", "--depth=1", "origin", base)
    return git("diff", "--unified=0", "--no-color", "--no-ext-diff", "-M", base, "HEAD").stdout


# ---------------------------------------------------------------------- the notes

def affected(notes: list[dict], touched: dict[str, list[tuple[int, int]]]) -> list[dict]:
    out = []
    for note in notes:
        status = note.get("status")
        if status not in HEADINGS:
            continue  # other scheme, skipped: not something this pull request did
        found = note.get("found_at") or {}
        paths = {note["recorded_at"]["path"], found.get("path")}
        if not any(p in touched for p in paths if p):
            continue
        if status != "ok":
            out.append(note)
            continue
        start, end = found.get("line"), found.get("end_line") or found.get("line")
        spans = touched.get(found.get("path"), [])
        if start and any(a <= end and start <= b for a, b in spans):
            out.append(note)
    out.sort(key=lambda n: (ORDER.index(n["status"]), n["recorded_at"]["path"],
                            n["recorded_at"]["line"]))
    return out


def _quiet_mentions(text: str) -> str:
    # A reason that says "ask @alice" should not page alice on every push.
    return re.sub(r"(^|[\s(])@(?=\w)", "\\1@\u200b", text)


def _quote(text: str) -> str:
    text = text.strip()
    if len(text) > MAX_REASON:
        text = text[:MAX_REASON].rstrip() + " …"
    return "\n".join("> " + line if line else ">" for line in _quiet_mentions(text).splitlines())


def _where(place: dict | None, link: str | None) -> str:
    if not place:
        return ""
    end = place.get("end_line")
    lines = f"{place['line']}" + (f"-{end}" if end and end != place["line"] else "")
    label = f"`{place['path']}:{lines}`"
    scope = place.get("scope")
    if scope and scope != "<module>":
        label += f" in `{scope}`"
    if link:
        anchor = f"#L{place['line']}" + (f"-L{end}" if end and end != place["line"] else "")
        label = f"[{label}]({link}/{urllib.parse.quote(place['path'])}{anchor})"
    return label


def render(notes: list[dict], blob_url: str | None) -> str:
    """The comment body. `blob_url` is where the pull request's files live, if known."""
    if not notes:
        return (f"{MARKER}\n**fence:** this pull request no longer touches any code "
                "with a recorded reason.\n")

    shown = notes[:MAX_NOTES]
    parts = [MARKER, "### Recorded reasons this pull request touches", "",
             "Someone wrote down why this code is the way it is. Worth reading before "
             "merging.", ""]
    for note in shown:
        status = note["status"]
        heading = HEADINGS[status]
        if status == "changed" and note.get("similarity") is not None:
            heading += f" ({note['similarity']:.0%} similar)"
        where = _where(note.get("found_at"), blob_url) if status != "removed" else ""
        if status in ("removed", "moved") or not where:
            recorded = _where(note["recorded_at"], None)
            where = f"was {recorded}" + (f", now {where}" if where else "")
        parts.append(f"**{heading}** · {where} · note `{note['id']}`")
        parts.append(_quote(note.get("reason") or ""))
        if note.get("source"):
            parts.append(f"> — {_quiet_mentions(note['source'])}")
        parts.append("")
    if len(notes) > len(shown):
        parts += [f"…and {len(notes) - len(shown)} more. `fence check` lists them all.", ""]

    statuses = {n["status"] for n in shown}
    steps = []
    if "removed" in statuses:
        steps.append('the reason no longer applies: `fence retire <id> -m "what changed"`')
        steps.append("the code moved where fence couldn't follow: "
                     "`fence reanchor <id> <file>:<line>`")
    if statuses & {"changed", "ambiguous", "ok"}:
        steps.append("the change keeps the reason true: `fence confirm <id>`")
    if statuses & {"moved", "renamed"}:
        steps.append("follow code that moved or was renamed: `fence update`")
    if steps:
        parts.append("<sub>If " + "; if ".join(steps) + ".</sub>")
    return "\n".join(parts) + "\n"


# ------------------------------------------------------------------------ posting

class GitHub:
    def __init__(self, api: str, repo: str, token: str):
        self.api, self.repo, self.token = api.rstrip("/"), repo, token

    def call(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"{self.api}{path}", data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "fence-pr-comment",
        })
        with urllib.request.urlopen(request, timeout=30) as response:
            text = response.read().decode() or "null"
            return json.loads(text)

    def existing(self, number: int) -> dict | None:
        page = 1
        while True:
            batch = self.call("GET", f"/repos/{self.repo}/issues/{number}/comments"
                                     f"?per_page=100&page={page}")
            for comment in batch:
                if MARKER in (comment.get("body") or ""):
                    return comment
            if len(batch) < 100:
                return None
            page += 1

    def post(self, number: int, body: str) -> str:
        found = self.existing(number)
        if found is None:
            if "### Recorded reasons" not in body:
                return "nothing to say"  # never was a warning, so no all-clear either
            self.call("POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": body})
            return "posted"
        if found.get("body") == body:
            return "unchanged"
        self.call("PATCH", f"/repos/{self.repo}/issues/comments/{found['id']}", {"body": body})
        return "updated"


def main() -> int:
    env = os.environ
    try:
        with open(env["CHECK_JSON"], encoding="utf-8") as handle:
            notes = json.load(handle).get("notes", [])
        touched = changed_lines(git_diff(env["BASE_SHA"]))
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"::warning::fence could not work out what this pull request touches: {error}")
        return 0

    hits = affected(notes, touched)
    head = env.get("HEAD_SHA")
    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    blob = f"{server}/{env['GITHUB_REPOSITORY']}/blob/{head}" if head else None
    body = render(hits, blob)
    print(f"fence: {len(hits)} note(s) touched by this pull request")

    github = GitHub(env.get("GITHUB_API_URL", "https://api.github.com"),
                    env["GITHUB_REPOSITORY"], env.get("GITHUB_TOKEN", ""))
    try:
        print(f"fence: comment {github.post(int(env['PR_NUMBER']), body)}")
    except (urllib.error.URLError, OSError, ValueError) as error:
        detail = getattr(error, "code", "") or error
        print(f"::warning::fence could not comment on the pull request ({detail}). "
              "The workflow needs `permissions: pull-requests: write`; pull requests "
              "from forks get a read-only token and cannot be commented on.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

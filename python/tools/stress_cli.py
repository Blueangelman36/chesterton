"""Stress a fence build through its command line, in any language it reads.

The harness next door imports the library and uses `ast`, which ties it to Python.
This one only runs the binary: it asks `fence statements` where the code is, edits
by line, and asks `fence check --json` what happened. That is what lets it test
TypeScript, and whatever comes after.

  comments   a comment line before every noted statement   (must not alarm)
  rename     one identifier renamed through a file         (must not alarm)
  move       a top-level statement cut into another file   (must not alarm)
  delete     the noted statement removed outright          (must alarm)
  edit       a literal inside it changed                   (must alarm)

Every change is made by line, and every verdict comes from the binary, so nothing
here knows what language it is looking at.

Usage:
  python tools/stress_cli.py <fence-binary> <repo> [--notes 20] [--suffix .ts,.tsx]
"""

import argparse
import json
import random
import re
import subprocess
import sys
from pathlib import Path

COMMENT = {".py": "#", ".pyi": "#"}
NOOP = {".py": "pass", ".pyi": "pass"}
KEYWORDS = {
    "const", "let", "var", "return", "function", "class", "import", "export", "from", "async",
    "await", "if", "else", "for", "while", "new", "this", "true", "false", "null", "undefined",
    "def", "self", "None", "True", "False", "and", "or", "not", "in", "is", "try", "except",
    "raise", "with", "as", "type", "interface", "string", "number", "boolean", "void", "Promise",
    "public", "private", "readonly", "extends", "implements", "yield", "throw", "case", "switch",
}
FOUND = {"ok", "renamed", "moved"}
ALARMED = {"changed", "ambiguous", "removed"}


class Fence:
    def __init__(self, binary: str, repo: Path):
        self.binary = binary
        self.repo = repo

    def __call__(self, *args) -> tuple[int, str]:
        done = subprocess.run([self.binary, *args], cwd=self.repo,
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        return done.returncode, done.stdout + done.stderr

    def statements(self, path: str) -> list[dict]:
        code, out = self(*["statements", path, "--json"])
        if code != 0:
            return []
        try:
            return json.loads(out)["statements"]
        except (ValueError, KeyError):
            return []

    def verdicts(self) -> dict[str, str]:
        _, out = self("check", "--json")
        try:
            return {note["id"]: note["status"] for note in json.loads(out)["notes"]}
        except (ValueError, KeyError):
            return {}


def git(repo: Path, *args) -> str:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    return done.stdout


def read(repo: Path, path: str) -> list[str]:
    """Bytes in, bytes out: a line ending inside a docstring or a template literal
    is part of the string, so rewriting CRLF as LF edits the code."""
    raw = (repo / path).read_bytes().decode("utf-8", errors="replace")
    return raw.split("\n")


def write(repo: Path, path: str, lines: list[str]) -> None:
    (repo / path).write_bytes("\n".join(lines).encode("utf-8"))


def like(neighbour: str, text: str) -> str:
    """An inserted line should end the way the lines around it do."""
    return text + "\r" if neighbour.endswith("\r") else text


def comment_for(path: str) -> str:
    return COMMENT.get(Path(path).suffix, "//")


def noop_for(path: str) -> str:
    return NOOP.get(Path(path).suffix, ";")


def place_notes(fence: Fence, files: list[str], wanted: int, seed: int) -> list[dict]:
    """Spread notes over files, one statement each, recorded through the binary."""
    rng = random.Random(seed)
    by_file = {}
    for path in files:
        usable = [s for s in fence.statements(path) if s["tokens"] >= 4]
        if usable:
            rng.shuffle(usable)
            by_file[path] = usable

    notes = []
    while by_file and len(notes) < wanted:
        for path in list(by_file):
            if not by_file[path]:
                del by_file[path]
                continue
            statement = by_file[path].pop()
            span = f"{path}:{statement['line']}-{statement['end_line']}"
            code, out = fence("add", span, "-m", f"why {path}:{statement['line']} is the way it is")
            if code != 0:
                continue  # a duplicate, or a statement the binary declined
            found = re.search(r"fence: added (\w+)", out)
            if found:
                notes.append(dict(statement, path=path, id=found.group(1)))
            if len(notes) >= wanted:
                break
    return notes


def scenario_comments(repo: Path, notes: list[dict]) -> list[dict]:
    """A comment above every noted statement, which shifts every line below it."""
    for path in sorted({note["path"] for note in notes}):
        lines = read(repo, path)
        marker = comment_for(path)
        for note in sorted([n for n in notes if n["path"] == path],
                           key=lambda n: n["line"], reverse=True):
            neighbour = lines[min(note["line"] - 1, len(lines) - 1)]
            lines.insert(note["line"] - 1, like(neighbour, f"{marker} a remark that changes nothing"))
        write(repo, path, lines)
    return notes


def scenario_rename(repo: Path, fence: Fence, notes: list[dict]) -> list[dict]:
    """Rename one identifier through the file holding the most notes."""
    path = max({n["path"] for n in notes}, key=lambda p: sum(n["path"] == p for n in notes))
    here = [n for n in notes if n["path"] == path]
    text = "\n".join(read(repo, path))
    words = [w for note in here for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", note["text"])]
    for word in sorted(set(words), key=len, reverse=True):
        if word in KEYWORDS or len(re.findall(rf"\b{re.escape(word)}\b", text)) < 2:
            continue
        write(repo, path, re.sub(rf"\b{re.escape(word)}\b", f"r_{word}", text).split("\n"))
        return here
    return []


def scenario_move(repo: Path, notes: list[dict]) -> list[dict]:
    """Cut a distinctive top-level statement and append it to another file."""
    movable = [n for n in notes if n["scope"] == "<module>" and n["tokens"] >= 12]
    if not movable:
        return []
    note = max(movable, key=lambda n: n["end_line"] - n["line"])
    others = [n["path"] for n in notes if n["path"] != note["path"]
              and Path(n["path"]).suffix == Path(note["path"]).suffix]
    if not others:
        return []

    lines = read(repo, note["path"])
    body = lines[note["line"] - 1:note["end_line"]]
    write(repo, note["path"], lines[:note["line"] - 1] + lines[note["end_line"]:])
    destination = others[0]
    write(repo, destination, read(repo, destination) + [""] + body)
    return [note]


def one_at_a_time(repo: Path, fence: Fence, notes: list[dict], edit) -> tuple[list, list]:
    """Apply one edit, ask, put the file back. The only way to attribute a verdict."""
    results, skipped = [], []
    for note in notes:
        lines = read(repo, note["path"])
        changed = edit(note, list(lines))
        if changed is None:
            skipped.append(note)
            continue
        write(repo, note["path"], changed)
        after = fence.statements(note["path"])
        if not after and changed != lines:
            # The edit broke the file. A real commit would not look like this.
            restore(repo)
            skipped.append(note)
            continue
        here = next((s for s in after if s["line"] == note["line"]), None)
        if here is not None and here["exact"] == note["exact"]:
            # The edit landed in a comment: nothing fence looks at changed, so
            # staying quiet is the right answer and there is nothing to test.
            restore(repo)
            skipped.append(note)
            continue
        verdict = fence.verdicts().get(note["id"], "missing")
        results.append((note, verdict))
        restore(repo)
    return results, skipped


def delete_edit(note: dict, lines: list[str]) -> list[str] | None:
    neighbour = lines[min(note["line"] - 1, len(lines) - 1)]
    del lines[note["line"] - 1:note["end_line"]]
    head = " " * (len(note["text"]) - len(note["text"].lstrip()))
    lines.insert(note["line"] - 1, like(neighbour, head + noop_for(note["path"])))
    return lines


def literal_edit(note: dict, lines: list[str]) -> list[str] | None:
    span = "\n".join(lines[note["line"] - 1:note["end_line"]])
    number = re.search(r"(?<![\w.])\d+(?![\w.])", span)
    if number:
        span = span[:number.start()] + str(int(number.group()) + 7) + span[number.end():]
    else:
        text = re.search(r"(['\"])([^'\"\n]*)\1", span)
        if not text:
            return None
        span = span[:text.end() - 1] + "_mutated" + span[text.end() - 1:]
    lines[note["line"] - 1:note["end_line"]] = span.split("\n")
    return lines


def restore(repo: Path) -> None:
    git(repo, "checkout", "--", ".")


def report(title: str, rule: set, results: list, skipped: list = ()) -> int:
    surprising = [(note, verdict) for note, verdict in results if verdict not in rule]
    note = f", {len(skipped)} not applicable" if skipped else ""
    verdict = "PASS" if not surprising else f"{len(surprising)} SURPRISING"
    print(f"\n{title}: {len(results)} note(s), {len(results) - len(surprising)} as expected"
          f"{note}, {verdict}")
    for item, got in surprising:
        print(f"  [{got}] {item['path']}:{item['line']} ({item['kind']}, {item['tokens']} tokens)")
        print(f"      {item['text']}")
    return len(surprising)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary")
    parser.add_argument("repo")
    parser.add_argument("--notes", type=int, default=20)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--suffix", default=".ts,.tsx")
    args = parser.parse_args()

    repo = Path(args.repo)
    fence = Fence(args.binary, repo)
    suffixes = tuple(args.suffix.split(","))
    files = [p for p in git(repo, "ls-files").split("\n") if p.strip().endswith(suffixes)]
    print(f"repo:   {repo}")
    print(f"files:  {len(files)} matching {args.suffix}")

    restore(repo)
    notes = place_notes(fence, files, args.notes, args.seed)
    print(f"notes:  {len(notes)} placed")
    if not notes:
        return "no notes could be placed; is this a language the binary reads?"
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "record the notes")

    surprises = 0
    for title, apply in (
        ("a comment above every statement -- must not alarm", lambda: scenario_comments(repo, notes)),
        ("one identifier renamed -- must not alarm", lambda: scenario_rename(repo, fence, notes)),
        ("a statement moved to another file -- must not alarm", lambda: scenario_move(repo, notes)),
    ):
        restore(repo)
        touched = apply()
        verdicts = fence.verdicts()
        results = [(note, verdicts.get(note["id"], "missing")) for note in touched]
        surprises += report(title, FOUND, results)
        restore(repo)

    results, skipped = one_at_a_time(repo, fence, notes, delete_edit)
    surprises += report("the statement deleted -- must alarm", ALARMED, results, skipped)

    results, skipped = one_at_a_time(repo, fence, notes, literal_edit)
    surprises += report("a literal edited -- must alarm", ALARMED, results, skipped)

    restore(repo)
    print(f"\ntotal surprising verdicts: {surprises}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

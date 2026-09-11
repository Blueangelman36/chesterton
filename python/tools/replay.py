"""Replay a repository's history against a set of notes.

Places notes on real statements at an early commit, walks forward through every
later commit, and reports what fence says about each note at each step. The
number that decides whether this tool is usable is how often it cries "removed"
about code that is still there in a different shape.

Two passes are run over the same notes:

  static      notes are never re-pinned: the worst case, drift accumulates
  maintained  notes are re-pinned whenever they're found, as `fence update` does

The repository is cloned first and only ever read.

Usage:  python tools/replay.py <repo> <clone-dir> [--notes 20] [--seed 0]
"""

import argparse
import random
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fence import anchor as A
from fence.gitutil import WorktreeReader, git

STATUSES = ["ok", "renamed", "moved", "changed", "ambiguous", "removed", "unparseable"]


def py_files(root):
    return [p for p in git("ls-files", "-z", cwd=root).split("\0") if p.endswith(".py")]


def sample_notes(root, count, seed):
    """Spread notes over files, taking statements at random within each."""
    rng = random.Random(seed)
    index = A.Index(WorktreeReader(root))
    by_file = {}
    for path in py_files(root):
        cands = index.get(path)
        if not cands:
            continue
        usable = [c for c in cands if len(c.tokens) >= 4]
        if usable:
            rng.shuffle(usable)
            by_file[path] = (cands, usable)

    notes = []
    while by_file and len(notes) < count:
        for path in list(by_file):
            cands, usable = by_file[path]
            if not usable:
                del by_file[path]
                continue
            target = usable.pop()
            notes.append({
                "path": path,
                "line": target.line,
                "end_line": target.end_line,
                "kind": target.kind,
                "tokens": len(target.tokens),
                "snippet": target.snippet,
                "anchor": A.make_anchor(target, cands,
                                        index.copies_elsewhere(path, target.exact, target.shape)),
            })
            if len(notes) >= count:
                break
    return notes


def replay(root, commits, notes, maintain):
    state = [dict(n) for n in notes]
    rows = []
    for sha in commits:
        git("checkout", "-q", sha, cwd=root)
        index = A.Index(WorktreeReader(root))
        counts = Counter()
        for note in state:
            m = A.locate(note["anchor"], index)
            note["status"] = m.how
            note["at"] = f"{m.candidate.path}:{m.candidate.line}" if m.candidate else ""
            counts[m.how] += 1
            if maintain and m.how in ("ok", "renamed", "moved"):
                found = m.candidate
                note["anchor"] = A.make_anchor(
                    found, index.get(found.path),
                    index.copies_elsewhere(found.path, found.exact, found.shape))
                note["snippet"] = found.snippet
        rows.append((sha, date_of(root, sha), counts))
    return rows, state


def still_present(root, note):
    """Does the note's first line still exist verbatim somewhere? A crude false-alarm flag."""
    wanted = first_line(note["snippet"])
    if not wanted:
        return False
    reader = WorktreeReader(root)
    for path in py_files(root):
        source = reader.read(path)
        if source and any(line.strip() == wanted for line in source.splitlines()):
            return True
    return False


def date_of(root, sha):
    return git("show", "-s", "--format=%cs", sha, cwd=root).strip()


def first_line(text):
    stripped = text.strip()
    return stripped.splitlines()[0] if stripped else ""


def print_table(title, rows):
    print(f"\n{title}")
    print("  #  commit   date        " + "".join(f"{s:>11}" for s in STATUSES))
    for i, (sha, date, counts) in enumerate(rows, start=2):
        cells = "".join(f"{counts[s] or '':>11}" for s in STATUSES)
        print(f"{i:>3}  {sha[:7]}  {date}{cells}")


def print_notes(root, title, state):
    interesting = [n for n in state if n["status"] != "ok"]
    print(f"\n{title}: {len(interesting)} note(s) not simply 'ok' at the final commit")
    for n in sorted(interesting, key=lambda n: n["status"]):
        flag = ""
        if n["status"] == "removed":
            flag = "  <-- first line still present verbatim" if still_present(root, n) else "  (gone)"
        print(f"  [{n['status']}] {n['path']}:{n['line']} ({n['kind']}, {n['tokens']} tokens){flag}")
        print(f"      {first_line(n['snippet'])}")
        if n["at"] and n["at"] != f"{n['path']}:{n['line']}":
            print(f"      now at {n['at']}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("repo")
    p.add_argument("clone")
    p.add_argument("--notes", type=int, default=20)
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()

    clone = Path(args.clone)
    if not clone.exists():
        subprocess.run(["git", "clone", "--quiet", str(Path(args.repo).resolve()), str(clone)], check=True)
    commits = git("rev-list", "--reverse", "HEAD", cwd=clone).split()
    if len(commits) < 2:
        sys.exit("need at least two commits to replay")

    git("checkout", "-q", commits[0], cwd=clone)
    notes = sample_notes(clone, args.notes, args.seed)
    if not notes:
        sys.exit("no Python statements to put notes on at the first commit")

    print(f"repo:   {args.repo}")
    print(f"clone:  {clone}")
    print(f"start:  {commits[0][:7]} ({date_of(clone, commits[0])}), "
          f"{len(py_files(clone))} python file(s), {len(notes)} notes placed")
    print(f"walk:   {len(commits) - 1} later commit(s)")

    for title, maintain in (("STATIC (notes never re-pinned)", False),
                            ("MAINTAINED (re-pinned whenever found, as `fence update` does)", True)):
        git("checkout", "-q", commits[0], cwd=clone)
        rows, state = replay(clone, commits[1:], notes, maintain)
        print_table(title, rows)
        print_notes(clone, title, state)


if __name__ == "__main__":
    main()

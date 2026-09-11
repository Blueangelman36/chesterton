"""Put notes on a real codebase, then apply the refactors that break line-based tools.

Real history is often too tame to test anchoring: if nothing moves, everything
"works". This applies changes with a known right answer to a checkout of real
code and reports where fence disagrees.

  reformat   every file rewritten from its AST (all formatting and comments gone)
  rename     identifiers renamed consistently through a file
  move       a function cut from one file and appended to another
  delete     the noted statement removed outright        (must be caught)
  edit       a literal inside the noted statement changed (must not pass as ok)

The first three must not raise an alarm; the last two must not stay silent.

Usage:  python tools/stress.py <clone-dir> [--notes 20] [--seed 0]
"""

import argparse
import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fence import anchor as A
from fence.gitutil import WorktreeReader, git
from replay import first_line, py_files, sample_notes

FOUND = {"ok", "renamed", "moved"}
ALARMED = {"changed", "ambiguous", "removed"}
MOVABLE = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def restore(root):
    git("checkout", "-q", "--", ".", cwd=root)


def locate_all(root, notes):
    index = A.Index(WorktreeReader(root))
    return [(n, A.locate(n["anchor"], index)) for n in notes]


def read(root, path):
    return (root / path).read_text(encoding="utf-8", errors="replace")


def write(root, path, text):
    (root / path).write_text(text, encoding="utf-8", newline="\n")


def parses(text):
    try:
        ast.parse(text)
        return True
    except (SyntaxError, ValueError):
        return False


def reformat(root, notes):
    """Rewrite every file from its AST: no original formatting or comments survive."""
    for path in py_files(root):
        try:
            write(root, path, ast.unparse(ast.parse(read(root, path))) + "\n")
        except (SyntaxError, ValueError):
            continue
    return notes


class _Rename(ast.NodeTransformer):
    def visit_Name(self, node):
        node.id = "r_" + node.id
        return node

    def visit_arg(self, node):
        node.arg = "r_" + node.arg
        self.generic_visit(node)
        return node


def rename(root, notes):
    """Rename identifiers throughout the file holding the most notes."""
    path = _busiest_file(notes)
    try:
        tree = _Rename().visit(ast.parse(read(root, path)))
    except (SyntaxError, ValueError):
        return []
    write(root, path, ast.unparse(ast.fix_missing_locations(tree)) + "\n")
    return [n for n in notes if n["path"] == path]


def move(root, notes):
    """Cut the function or class holding the most notes and append it to another file."""
    best = None
    for path in sorted({n["path"] for n in notes}):
        try:
            tree = ast.parse(read(root, path))
        except (SyntaxError, ValueError):
            continue
        for node in tree.body:
            if not isinstance(node, MOVABLE):
                continue
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            inside = [n for n in notes
                      if n["path"] == path and start <= n["line"] <= node.end_lineno]
            if inside and (best is None or len(inside) > len(best[3])):
                best = (path, start, node.end_lineno, inside)
    if best is None:
        return []

    path, start, end, moved_notes = best
    lines = read(root, path).split("\n")
    body = "\n".join(lines[start - 1:end])
    kept = "\n".join(lines[:start - 1] + lines[end:])
    others = [p for p in py_files(root) if p != path]
    if not others or not parses(kept):
        return []
    write(root, path, kept)
    write(root, others[0], read(root, others[0]).rstrip("\n") + "\n\n\n" + body + "\n")
    return moved_notes


def delete_each(root, notes):
    """One note at a time: delete exactly the statement it points at.

    One index, re-reading only the file just edited. Rebuilding it per note meant
    parsing the whole repository once per note, which on a few hundred files is
    the difference between seconds and many minutes.
    """
    index = A.Index(WorktreeReader(root))
    results, skipped, dirty = [], [], None
    for note in notes:
        restore(root)
        if dirty:
            index.invalidate(dirty)
            dirty = None
        lines = read(root, note["path"]).split("\n")
        head = lines[note["line"] - 1]
        indent = " " * (len(head) - len(head.lstrip()))
        del lines[note["line"] - 1:note["end_line"]]
        text = "\n".join(lines)
        if not parses(text):
            # Deleting the only statement of a block leaves a syntax error that no
            # real commit would contain; stand a `pass` in its place instead.
            lines.insert(note["line"] - 1, indent + "pass")
            text = "\n".join(lines)
        if not parses(text):
            skipped.append(note)
            continue
        write(root, note["path"], text)
        index.invalidate(note["path"])
        dirty = note["path"]
        results.append((note, A.locate(note["anchor"], index)))
    return results, skipped


def edit_each(root, notes):
    """One note at a time: change a literal inside the noted statement."""
    index = A.Index(WorktreeReader(root))
    results, skipped, dirty = [], [], None
    for note in notes:
        restore(root)
        if dirty:
            index.invalidate(dirty)
            dirty = None
        lines = read(root, note["path"]).split("\n")
        span = lines[note["line"] - 1:note["end_line"]]
        edited = _mutate("\n".join(span))
        if edited is None:
            skipped.append(note)
            continue
        lines[note["line"] - 1:note["end_line"]] = edited.split("\n")
        text = "\n".join(lines)
        if not parses(text):
            skipped.append(note)
            continue
        write(root, note["path"], text)
        index.invalidate(note["path"])
        dirty = note["path"]
        results.append((note, A.locate(note["anchor"], index)))
    return results, skipped


def _mutate(text):
    number = re.search(r"(?<![\w.])\d+(?![\w.])", text)
    if number:
        return text[:number.start()] + str(int(number.group()) + 7) + text[number.end():]
    string = re.search(r"(['\"])([^'\"\n]*)\1", text)
    if string:
        return text[:string.end() - 1] + "_mutated" + text[string.end() - 1:]
    return None


def _busiest_file(notes):
    counts = {}
    for n in notes:
        counts[n["path"]] = counts.get(n["path"], 0) + 1
    return max(counts, key=counts.get)


def report(title, rule, results, skipped=()):
    bad = [(n, m) for n, m in results if m.how not in rule]
    verdict = "PASS" if not bad else f"{len(bad)} SURPRISING"
    note = f", {len(skipped)} not applicable" if skipped else ""
    print(f"\n{title}: {len(results)} note(s), {len(results) - len(bad)} as expected{note}, {verdict}")
    for n, m in bad:
        print(f"  [{m.how}] {n['path']}:{n['line']} ({n['kind']}, {n['tokens']} tokens)")
        print(f"      {first_line(n['snippet'])}")
    return len(bad)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("clone")
    p.add_argument("--notes", type=int, default=20)
    p.add_argument("--seed", type=int, default=0)
    args = p.parse_args()

    root = Path(args.clone)
    restore(root)
    notes = sample_notes(root, args.notes, args.seed)
    print(f"clone: {root}")
    print(f"notes: {len(notes)} placed at {git('rev-parse', '--short', 'HEAD', cwd=root).strip()}")

    surprises = 0
    for title, transform in (("reformat (rewritten from AST)", reformat),
                             ("rename identifiers", rename),
                             ("move a function to another file", move)):
        restore(root)
        applicable = transform(root, notes)
        surprises += report(title + " -- must not alarm", FOUND, locate_all(root, applicable))

    restore(root)
    results, skipped = delete_each(root, notes)
    surprises += report("delete the statement -- must alarm", ALARMED, results, skipped)

    restore(root)
    results, skipped = edit_each(root, notes)
    surprises += report("edit a literal -- must not stay silent", ALARMED, results, skipped)
    restore(root)

    print(f"\ntotal surprising verdicts: {surprises}")


if __name__ == "__main__":
    main()

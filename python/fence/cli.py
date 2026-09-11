"""fence: remember why code exists, and speak up before it's removed."""

import argparse
import sys
from collections import Counter
from pathlib import Path

from . import FenceError
from . import anchor as A
from .gitutil import (GitError, IndexReader, WorktreeReader, blame, git,
                      hooks_dir, repo_root, staged_paths)
from .store import Store

HOOK_MARKER = "# installed by fence"

LABELS = {
    "ok": "ok",
    "renamed": "renamed",
    "moved": "moved",
    "changed": "changed",
    "ambiguous": "ambiguous",
    "removed": "REMOVED",
    "unparseable": "skipped",
}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass
    args = _parser().parse_args(argv)
    try:
        return args.run(args)
    except (FenceError, GitError) as e:
        print(f"fence: {e}", file=sys.stderr)
        return 2


def cmd_init(args) -> int:
    root = repo_root()
    Store(root).dir.mkdir(exist_ok=True)
    hook = hooks_dir(root) / "pre-commit"
    command = _hook_command()
    if hook.exists() and HOOK_MARKER not in hook.read_text(encoding="utf-8", errors="replace"):
        print(f"fence: {hook} already exists. Add this line to it to enable fence:\n  {command}")
        return 1
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(f"#!/bin/sh\n{HOOK_MARKER}\n"
                    f"# Blocks commits that delete code with a recorded reason.\n{command}\n",
                    encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    print('fence: installed pre-commit hook. Record a reason with: fence add <file>:<line> -m "why"')
    return 0


def cmd_add(args) -> int:
    root = repo_root()
    path, start, end = _parse_loc(args.loc, root)
    index, target = _pick(root, path, start, end)
    reason, source = args.message, args.source
    if args.from_blame:
        sha, message = blame(root, path, start)
        reason = reason or message
        source = source or f"commit {sha[:12]}"
    if not reason or not reason.strip():
        raise FenceError('say why with -m "...", or use --from-blame to borrow the message '
                         "of the commit that wrote the line")
    note = Store(root).create(reason.strip(), source, _author(root), target.snippet,
                              _anchor(index, target))
    _stage(root)
    print(f"fence: added {note['id']}  {path}:{target.line}  in {A.scope_label(target.scope)}")
    print(f"    {_first_line(target.snippet)}")
    return 0


def cmd_list(args) -> int:
    root = repo_root()
    notes = Store(root).notes()
    if args.path:
        prefix = _repo_path(args.path, root)
        notes = [n for n in notes if n["anchor"]["path"] == prefix
                 or n["anchor"]["path"].startswith(prefix.rstrip("/") + "/")]
    if not notes:
        print('fence: no notes yet. Add one with: fence add <file>:<line> -m "why"')
    for n in notes:
        a = n["anchor"]
        print(f"{n['id']}  {a['path']}:{a['line']}  in {A.scope_label(a['scope'])}")
        print(f"    why:  {_first_line(n['reason'])}")
        if n.get("source"):
            print(f"    from: {n['source']}")
    return 0


def cmd_check(args) -> int:
    root = repo_root()
    notes = Store(root).notes()
    if args.staged:
        # Only what this commit touches: never nag about code nobody is editing.
        touched = set(staged_paths(root))
        notes = [n for n in notes if n["anchor"]["path"] in touched]
        index = A.Index(IndexReader(root))
    else:
        index = A.Index(WorktreeReader(root))

    results = [(n, A.locate(n["anchor"], index)) for n in notes]
    shown = [(n, m) for n, m in results if not (args.staged and m.how == "ok")]
    for note, m in shown:
        print("\n".join(_describe(note, m)))

    counts = Counter(m.how for _, m in results)
    if not args.staged or shown:
        tally = ", ".join(f"{counts[k]} {LABELS[k]}" for k in LABELS if counts[k])
        print(f"\nfence: {len(results)} note(s) checked" + (f": {tally}" if tally else ""))
        for hint in _hints(counts, args.staged):
            print(f"  {hint}")
    return 1 if counts["removed"] else 0


def cmd_update(args) -> int:
    root = repo_root()
    store = Store(root)
    index = A.Index(WorktreeReader(root))
    updated = 0
    for note in store.notes():
        m = A.locate(note["anchor"], index)
        if m.how not in ("ok", "renamed", "moved"):
            continue
        c = m.candidate
        if _reanchor(store, note, c, index):
            updated += 1
            if m.how != "ok":
                print(f"fence: {note['id']} now follows {c.path}:{c.line}  in {A.scope_label(c.scope)}")
    if updated:
        _stage(root)
    print(f"fence: refreshed {updated} note(s)")
    return 0


def cmd_confirm(args) -> int:
    root = repo_root()
    store = Store(root)
    note = store.get(args.id)
    index = A.Index(WorktreeReader(root))
    c = A.locate(note["anchor"], index).candidate
    if c is None:
        raise FenceError(f"can't find the code for {note['id']}; "
                         f"point at it with: fence reanchor {note['id']} <file>:<line>")
    _reanchor(store, note, c, index)
    _stage(root)
    print(f"fence: {note['id']} confirmed at {c.path}:{c.line}")
    return 0


def cmd_reanchor(args) -> int:
    root = repo_root()
    store = Store(root)
    note = store.get(args.id)
    path, start, end = _parse_loc(args.loc, root)
    index, target = _pick(root, path, start, end)
    _reanchor(store, note, target, index)
    _stage(root)
    print(f"fence: {note['id']} now points at {path}:{target.line}  in {A.scope_label(target.scope)}")
    print(f"    {_first_line(target.snippet)}")
    return 0


def cmd_retire(args) -> int:
    root = repo_root()
    store = Store(root)
    note = store.get(args.id)
    store.retire(note, args.message.strip())
    _stage(root)
    print(f"fence: retired {note['id']} (kept in .fence/retired/)")
    return 0


def _describe(note: dict, m: A.Match) -> list[str]:
    a, c = note["anchor"], m.candidate
    label = LABELS[m.how] + (f" {m.similarity:.0%}" if m.how == "changed" else "")
    if c is None:
        where = f"{a['path']}:{a['line']}  in {A.scope_label(a['scope'])}"
    elif m.how == "moved":
        where = f"{a['path']}:{a['line']} -> {c.path}:{c.line}  in {A.scope_label(c.scope)}"
    else:
        where = f"{c.path}:{c.line}  in {A.scope_label(c.scope)}"
    lines = [f"[{label}] {note['id']}  {where}", f"    why: {_first_line(note['reason'])}"]
    if m.how == "removed":
        lines.append(f"    was: {_first_line(note['snippet'])}")
    elif m.how == "changed":
        was, now = _first_difference(note["snippet"], c.snippet)
        lines += [f"    was: {was}", f"    now: {now}"]
    elif m.how == "ambiguous":
        lines.append("    an identical copy of this statement was removed; "
                     "can't tell which one this note is about")
    elif m.how == "unparseable":
        lines.append(f"    {a['path']} doesn't parse right now, so this note wasn't checked")
    return lines


def _hints(counts: Counter, staged: bool) -> list[str]:
    hints = []
    if counts["removed"]:
        hints += ['reason no longer applies?    fence retire <id> -m "what changed"',
                  "code lives somewhere new?    fence reanchor <id> <file>:<line>"]
        if staged:
            hints.append("commit anyway?               git commit --no-verify")
    if counts["changed"] or counts["ambiguous"]:
        hints.append("reason still holds?          fence confirm <id>")
    if counts["renamed"] or counts["moved"]:
        hints.append("follow code that moved:      fence update")
    return hints


def _anchor(index: A.Index, target: A.Candidate) -> dict:
    """Anchors count the copies of a statement elsewhere, so the whole repo is indexed."""
    return A.make_anchor(target, index.get(target.path),
                         index.copies_elsewhere(target.path, target.exact, target.shape))


def _reanchor(store: Store, note: dict, target: A.Candidate, index: A.Index) -> bool:
    anchor = _anchor(index, target)
    if anchor == note["anchor"] and target.snippet == note["snippet"]:
        return False
    note["anchor"], note["snippet"] = anchor, target.snippet
    store.save(note)
    return True


def _pick(root: Path, path: str, start: int, end: int):
    reader = WorktreeReader(root)
    source = reader.read(path)
    if source is None:
        raise FenceError(f"{path}: no such file")
    # Take the target from the index, so it is the same object as its siblings.
    index = A.Index(reader)
    cands = index.get(path)
    if path in index.broken:
        A.candidates(path, source)  # re-raise with the parse error in the message
    target = A.pick(cands, start, end)
    if target is None:
        span = f"{start}-{end}" if end != start else str(start)
        raise FenceError(f"no statement covers {path}:{span}")
    return index, target


def _parse_loc(loc: str, root: Path) -> tuple[str, int, int]:
    path, sep, span = loc.rpartition(":")
    first, _, last = span.partition("-")
    try:
        start = int(first)
        end = int(last) if last else start
    except ValueError:
        start = end = 0
    if not sep or not path or start < 1 or end < start:
        raise FenceError(f"expected <file>:<line> or <file>:<start>-<end>, got {loc!r}")
    return _repo_path(path, root), start, end


def _repo_path(path: str, root: Path) -> str:
    try:
        return (Path.cwd() / path).resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        raise FenceError(f"{path} is outside the repository") from None


def _hook_command() -> str:
    package_parent = Path(__file__).resolve().parent.parent.as_posix()
    python = Path(sys.executable).as_posix()
    return f'PYTHONPATH="{package_parent}" "{python}" -m fence check --staged || exit 1'


def _author(root: Path) -> str | None:
    return git("config", "user.name", cwd=root, check=False).strip() or None


def _stage(root: Path) -> None:
    git("add", "-A", "--", ".fence", cwd=root)


def _first_line(text: str) -> str:
    stripped = text.strip()
    return stripped.splitlines()[0] if stripped else ""


def _first_difference(was: str, now: str) -> tuple[str, str]:
    for a, b in zip(was.splitlines(), now.splitlines()):
        if a.strip() != b.strip():
            return a.strip(), b.strip()
    return _first_line(was), _first_line(now)


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fence", description=__doc__.split(": ", 1)[1])
    sub = p.add_subparsers(dest="command", required=True, metavar="<command>")

    s = sub.add_parser("init", help="install the pre-commit hook")
    s.set_defaults(run=cmd_init)

    s = sub.add_parser("add", help="record why a statement exists")
    s.add_argument("loc", metavar="FILE:LINE[-END]")
    s.add_argument("-m", "--message", help="the reason")
    s.add_argument("--from-blame", action="store_true",
                   help="borrow the message of the commit that wrote the line")
    s.add_argument("--source", help="where the reason came from (URL, ticket, commit)")
    s.set_defaults(run=cmd_add)

    s = sub.add_parser("list", help="list notes, optionally under a path")
    s.add_argument("path", nargs="?")
    s.set_defaults(run=cmd_list)

    s = sub.add_parser("check", help="find each note's code and report what happened to it")
    s.add_argument("--staged", action="store_true",
                   help="check staged files only (what the pre-commit hook runs)")
    s.set_defaults(run=cmd_check)

    s = sub.add_parser("update", help="re-pin notes whose code was renamed or moved")
    s.set_defaults(run=cmd_update)

    s = sub.add_parser("confirm", help="the code changed, but the reason still holds")
    s.add_argument("id")
    s.set_defaults(run=cmd_confirm)

    s = sub.add_parser("reanchor", help="point a note at different code")
    s.add_argument("id")
    s.add_argument("loc", metavar="FILE:LINE[-END]")
    s.set_defaults(run=cmd_reanchor)

    s = sub.add_parser("retire", help="the reason no longer applies")
    s.add_argument("id")
    s.add_argument("-m", "--message", required=True, help="what changed")
    s.set_defaults(run=cmd_retire)
    return p

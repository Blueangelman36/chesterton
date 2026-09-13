"""fence: remember why code exists, and speak up before it's removed."""

import argparse
import json
import os
import sys
import traceback
from collections import Counter
from pathlib import Path

from . import FenceError
from . import anchor as A
from . import suggest as suggestions
from .cache import Cache
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
    "foreign": "other tool",
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
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        # A traceback in the middle of someone's commit says nothing they can act
        # on. Still non-zero: if the check could not run, it did not pass.
        print(f"fence: internal error ({type(e).__name__}: {e})", file=sys.stderr)
        print("fence: that is a bug in fence, not in your repository. The commit is "
              "blocked because the check could not run; git commit --no-verify goes "
              "ahead. Set FENCE_DEBUG=1 for the traceback.", file=sys.stderr)
        if os.environ.get("FENCE_DEBUG"):
            traceback.print_exc()
        return 3


AGENTS_MARKER = "<!-- fence -->"

AGENTS_BLOCK = """<!-- fence -->
## Why code exists

Some code in this repository has a recorded reason. Before deleting or rewriting
code you did not write, ask for it:

```bash
fence why <file>            # or <file>:<line>; --json for machine-readable output
```

If a commit is blocked, the message names a note id, and the code was kept
deliberately. Either `fence retire <id> -m "what changed"` when the reason no
longer applies, or `fence reanchor <id> <file>:<line>` when the code moved
somewhere the tool could not follow it. `git commit --no-verify` skips the check
without recording anything, so prefer either of the other two.

When you find out why something non-obvious has to be the way it is, write it
down where the next reader will meet it:

```bash
fence add <file>:<line> -m "why"
```
<!-- /fence -->
"""

SKILL = """---
name: fence
description: Look up why code exists before changing or deleting it, and resolve a commit blocked by a recorded reason. Use when editing unfamiliar code, when a pre-commit hook mentions a fence note id, or when someone asks why a guard, retry, sleep or workaround is there.
---

# fence

`fence` records why code exists and blocks commits that delete it. Notes are pinned
to statements, so they follow the code through reformatting, renames and moves.

## Before changing code you did not write

```bash
fence why src/client.py          # every recorded reason in the file
fence why src/client.py:42       # just the ones covering that line
fence why src/client.py --json   # same, machine-readable
```

A reason is evidence about the code's past that is not in the code. Take it
seriously: the usual failure this prevents is deleting a guard that looks
redundant and is not.

## When a commit is blocked

The hook prints the note id, the reason, and the code that is going away. Decide
which of these is true, and do not reach for `--no-verify` to get past it:

- the reason no longer applies: `fence retire <id> -m "what changed"`
- the code moved and fence could not follow: `fence reanchor <id> <file>:<line>`
- the code changed but the reason still holds: `fence confirm <id>`

## When you learn something

```bash
fence add <file>:<line> -m "why this has to be this way"
fence add <file>:<line> --from-blame     # borrow the commit message that wrote it
```

Record the reason, not the behaviour. "Retries twice" is in the code already;
"the vendor returns 200 on auth failure" is not.

## Verdicts

`ok` and `renamed` and `moved` pass. `changed` and `ambiguous` warn: the code was
edited, so check whether the reason still holds. `removed` blocks the commit.
"""

FENCE_README = """# .fence

These files record **why** some code exists, so a commit that deletes it says so
before it lands.

- `notes/` — one file per recorded reason, committed and reviewed with the code.
- `retired/` — reasons that no longer apply, kept because why a fence came down is
  worth knowing too.
- `cache.json` — derived data, ignored by git. Safe to delete at any time.

In a note, `reason` is the part a person wrote. The `anchor` below it is written by
the tool: fingerprints of the statement the note is pinned to, so the code can be
found again after it is reformatted, renamed, or moved to another file.

**If a commit of yours was just blocked,** the message named the note and offered
three ways forward: `fence retire <id> -m "what changed"` if the reason no longer
applies, `fence reanchor <id> <file>:<line>` if the code moved somewhere the tool
could not follow, or `git commit --no-verify` to go ahead regardless.

**If nothing ever blocks you,** the hook is not installed. Hooks live in
`.git/hooks/`, which git does not clone, so each working copy needs `fence init`
once. A `fence check` step in CI covers everyone regardless.
"""


def cmd_init(args) -> int:
    root = repo_root()
    Store(root).dir.mkdir(exist_ok=True)
    _explain(Store(root).dir)
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
    if args.agents:
        for written in _install_agent_docs(root):
            print(f"fence: wrote {written}")
    else:
        print("fence: `fence init --agents` also tells coding agents the notes are here.")
    return 0


def cmd_why(args) -> int:
    root = repo_root()
    target, start, end = _parse_target(args.loc, root)
    notes = [n for n in Store(root).notes() if _under(n["anchor"]["path"], target)]
    index = _worktree_index(root)

    found = []
    for note in notes:
        match = A.locate(note["anchor"], index)
        first = match.candidate.line if match.candidate else note["anchor"]["line"]
        last = match.candidate.end_line if match.candidate else first
        if start is not None and (last < start or first > end):
            continue
        found.append((note, match))

    if args.json:
        print(json.dumps({"notes": [_as_json(n, m) for n, m in found]}, indent=2))
        return 0
    if not found:
        where = target if start is None else f"{target}:{start}"
        print(f"fence: nothing recorded for {where}")
        return 0
    for note, match in found:
        anchor, c = note["anchor"], match.candidate
        location = f"{c.path}:{c.line}" if c else f"{anchor['path']}:{anchor['line']}"
        scope = A.scope_label(c.scope if c else anchor["scope"])
        print(f"{location}  in {scope}  [{LABELS[match.how]}]  {note['id']}")
        print(_block(note["reason"], "why: "))
        if note.get("source"):
            print(_block(note["source"], "from:"))
        print(f"    code: {_first_line(note['snippet'])}")
    return 0


def cmd_add(args) -> int:
    root = repo_root()
    path, start, end = _parse_loc(args.loc, root)
    index, target = _pick(root, path, start, end)
    store = Store(root)
    if not args.also:
        _refuse_duplicate(store, index, target, path)
    reason, source = args.message, args.source
    if args.from_blame:
        sha, message = blame(root, path, start)
        reason = reason or message
        source = source or f"commit {sha[:12]}"
    if not reason or not reason.strip():
        raise FenceError('say why with -m "...", or use --from-blame to borrow the message '
                         "of the commit that wrote the line")
    note = store.create(reason.strip(), source, _author(root), target.snippet,
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


def cmd_suggest(args) -> int:
    """What to record, for a repository that has recorded nothing yet."""
    root = repo_root()
    index = _worktree_index(root)
    taken = set()
    for note in Store(root).notes():
        found = A.locate(note["anchor"], index).candidate
        if found:
            taken.add((found.path, found.line, found.end_line))

    reader = WorktreeReader(root)
    paths = [p for p in reader.paths() if p.endswith(".py")
             and A.worth_reading(p, reader.read(p) or "")]
    if args.path:
        prefix = _repo_path(args.path, root)
        paths = [p for p in paths if _under(p, prefix)]
    found = suggestions.collect(index, paths, taken, args.limit)

    if args.json:
        print(json.dumps({"suggestions": [
            {"path": s.path, "line": s.line, "end_line": s.end_line, "kind": s.kind,
             "scope": s.scope, "text": s.text, "score": s.score, "reasons": s.reasons,
             "draft": s.draft, "command": s.command}
            for s in found]}, indent=2))
        return 0
    if not found:
        print("fence: nothing stood out. That is not the same as nothing being worth recording.")
        return 0
    print(f"fence: {len(found)} statement(s) whose reason may not be written down, worst first.")
    print("The draft reason is somewhere to start, not something to accept.\n")
    for item in found:
        print(f"  {item.path}:{item.line}  in {item.scope}")
        print(f"      {item.text}")
        for reason in (item.reasons if args.why else item.reasons[:1]):
            print(f"      {reason}")
        print(f"      {item.command}\n")
    return 0


def cmd_statements(args) -> int:
    """Every statement a note could be pinned to, for tools rather than people.

    This is what lets a test harness work on a language it cannot parse itself:
    ask the command where the statements are, then edit by line.
    """
    root = repo_root()
    path = _repo_path(args.path, root)
    source = WorktreeReader(root).read(path)
    if source is None:
        raise FenceError(f"{path}: no such file")
    found = A.candidates(path, source)
    if args.json:
        print(json.dumps({"path": path, "statements": [
            {"line": c.line, "end_line": c.end_line, "kind": c.kind,
             "scope": A.scope_label(c.scope), "tokens": len(c.tokens),
             "exact": c.exact, "shape": c.shape, "text": _first_line(c.snippet)}
            for c in found]}, indent=2))
        return 0
    for c in found:
        print(f"{c.line}:{c.end_line}  {c.kind}  in {A.scope_label(c.scope)}  ({len(c.tokens)} tokens)")
    return 0


def cmd_check(args) -> int:
    root = repo_root()
    notes = Store(root).notes()
    if args.staged:
        # Only what this commit touches: never nag about code nobody is editing.
        touched = set(staged_paths(root))
        notes = [n for n in notes if n["anchor"]["path"] in touched]
        # The cache is keyed by content, so staged files that nobody edited hit
        # it anyway. Read-only: a commit shouldn't be writing files.
        index = A.Index(IndexReader(root), Cache(root, A.ANCHOR_VERSION, writable=False))
    else:
        index = _worktree_index(root)

    results = [(n, A.locate(n["anchor"], index)) for n in notes]
    if args.json:
        counted = Counter(m.how for _, m in results)
        print(json.dumps({
            "checked": len(results),
            "counts": {k: counted[k] for k in LABELS if counted[k]},
            "blocked": bool(counted["removed"]),
            "notes": [_as_json(n, m) for n, m in results],
        }, indent=2))
        return 1 if counted["removed"] else 0

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
    index = _worktree_index(root)
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
    index = _worktree_index(root)
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
    elif m.how == "foreign":
        lines.append(f"    written by another implementation (anchor version {a.get('version')}), "
                     "so its fingerprints mean nothing here")
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
    if counts["foreign"]:
        hints.append("written by another build:    fence update re-pins them here (docs/FORMAT.md)")
    return hints


def _refuse_duplicate(store: Store, index: A.Index, target: A.Candidate, path: str) -> None:
    """Two notes on one statement is nearly always a re-run, and occasionally meant.

    Compares where notes are *now*, not where they were recorded, so a note whose
    code has since moved onto this statement counts as the same one.
    """
    nearby = [n for n in store.notes()
              if n["anchor"]["path"] == target.path or n["anchor"]["exact"] == target.exact]
    here = []
    for note in nearby:
        found = A.locate(note["anchor"], index).candidate
        if found and (found.path, found.line, found.exact) == (target.path, target.line, target.exact):
            here.append(note)
    if not here:
        return
    listed = "\n  ".join(f"{n['id']}  {_first_line(n['reason'])}" for n in here)
    raise FenceError(f"{path}:{target.line} already has a reason recorded:\n  {listed}\n"
                     "  --also records a second, independent reason for the same code")


def _as_json(note: dict, match: A.Match) -> dict:
    """The shape agents read. Keep it stable: something out there parses it."""
    anchor, c = note["anchor"], match.candidate
    return {
        "id": note["id"],
        "status": match.how,
        "reason": note["reason"],
        "source": note.get("source"),
        "author": note.get("author"),
        "created": note.get("created"),
        "similarity": round(match.similarity, 3) if match.how == "changed" else None,
        "recorded_at": {"path": anchor["path"], "line": anchor["line"],
                        "scope": A.scope_label(anchor["scope"])},
        "found_at": ({"path": c.path, "line": c.line, "end_line": c.end_line,
                      "scope": A.scope_label(c.scope)} if c else None),
        "snippet": note["snippet"],
    }


def _block(text: str, label: str) -> str:
    lines = text.strip().splitlines() or [""]
    following = "".join(f"\n    {' ' * len(label)} {line}" for line in lines[1:])
    return f"    {label} {lines[0]}{following}"


def _parse_target(loc: str, root: Path) -> tuple[str, int | None, int | None]:
    """A file or directory, optionally narrowed to a line or a range."""
    _, sep, span = loc.rpartition(":")
    if sep and span and span.replace("-", "").isdigit():
        return _parse_loc(loc, root)
    return _repo_path(loc, root), None, None


def _under(path: str, target: str) -> bool:
    return path == target or path.startswith(target.rstrip("/") + "/")


def _install_agent_docs(root: Path) -> list[str]:
    """Discovery is the bottleneck: a tool an agent never hears about may as well not exist."""
    written = []
    agents = root / "AGENTS.md"
    existing = agents.read_text(encoding="utf-8", errors="replace") if agents.exists() else ""
    if AGENTS_MARKER not in existing:
        separator = "\n\n" if existing.strip() else ""
        agents.write_text(existing.rstrip("\n") + separator + AGENTS_BLOCK,
                          encoding="utf-8", newline="\n")
        written.append("AGENTS.md")
    skill = root / ".claude" / "skills" / "fence" / "SKILL.md"
    if not skill.exists():
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text(SKILL, encoding="utf-8", newline="\n")
        written.append(str(skill.relative_to(root)).replace("\\", "/"))
    return written


def _explain(fence_dir: Path) -> None:
    """Leave an explanation for whoever meets .fence in a pull request cold."""
    readme = fence_dir / "README.md"
    if not readme.exists():
        readme.write_text(FENCE_README, encoding="utf-8", newline="\n")
    ignore = fence_dir / ".gitignore"
    if not ignore.exists():
        ignore.write_text("cache.json\ncache.json.tmp\n", encoding="utf-8", newline="\n")


def _worktree_index(root: Path) -> A.Index:
    """Files as they are on disk, with the parse cache, which is keyed to them."""
    return A.Index(WorktreeReader(root), Cache(root, A.ANCHOR_VERSION))


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
    index = A.Index(reader, Cache(root, A.ANCHOR_VERSION))
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
    s.add_argument("--agents", action="store_true",
                   help="also tell coding agents: an AGENTS.md block and a Claude Code skill")
    s.set_defaults(run=cmd_init)

    s = sub.add_parser("why", help="what reasons are recorded for this file or line")
    s.add_argument("loc", metavar="FILE[:LINE[-END]]")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(run=cmd_why)

    s = sub.add_parser("add", help="record why a statement exists")
    s.add_argument("loc", metavar="FILE:LINE[-END]")
    s.add_argument("-m", "--message", help="the reason")
    s.add_argument("--from-blame", action="store_true",
                   help="borrow the message of the commit that wrote the line")
    s.add_argument("--source", help="where the reason came from (URL, ticket, commit)")
    s.add_argument("--also", action="store_true",
                   help="record another reason for a statement that already has one")
    s.set_defaults(run=cmd_add)

    s = sub.add_parser("list", help="list notes, optionally under a path")
    s.add_argument("path", nargs="?")
    s.set_defaults(run=cmd_list)

    s = sub.add_parser("suggest", help="statements whose reason is probably not written down")
    s.add_argument("path", nargs="?")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--why", action="store_true", help="every signal, not just the strongest")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(run=cmd_suggest)

    s = sub.add_parser("statements", help="every statement a note could be pinned to, for tooling")
    s.add_argument("path")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(run=cmd_statements)

    s = sub.add_parser("check", help="find each note's code and report what happened to it")
    s.add_argument("--staged", action="store_true",
                   help="check staged files only (what the pre-commit hook runs)")
    s.add_argument("--json", action="store_true", help="machine-readable output")
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

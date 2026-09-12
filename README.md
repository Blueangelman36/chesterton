# chesterton

[![CI](https://github.com/Blueangelman36/chesterton/actions/workflows/ci.yml/badge.svg)](https://github.com/Blueangelman36/chesterton/actions/workflows/ci.yml)

*Don't take down a fence until you know why it was put up.*

`fence` remembers **why** code exists, and speaks up when a commit is about to delete it.

Some of the most painful bugs start the same way: someone removes a strange-looking retry loop,
a `sleep(50)`, or an `if` that "can't happen", and it turns out it could. The person who wrote it
knew why. That reason lived in a closed PR, a Slack thread, or their head, and none of those are
open when the next person edits the file.

`fence` keeps the reason next to the code, follows the code through refactors, and interrupts
only at the moment it matters: when a commit removes something that has a recorded reason.

## Try it

No dependencies. Python 3.10+.

```bash
git clone https://github.com/Blueangelman36/chesterton
pip install -e chesterton/python  # or run it in place: python -m fence
```

```text
$ fence init
fence: installed pre-commit hook. Record a reason with: fence add <file>:<line> -m "why"

$ fence add client.py:4 -m "Vendor API returns 200 on auth failure; the error is only in the body."
fence: added 1559744b  client.py:4  in Client.fetch
    if resp.json().get("error") == "unauthorized":
```

Later, someone deletes the check:

```text
$ git commit -m "Simplify fetch"
[REMOVED] 1559744b  client.py:4  in Client.fetch
    why: Vendor API returns 200 on auth failure; the error is only in the body.
    was: if resp.json().get("error") == "unauthorized":

fence: 1 note(s) checked: 1 REMOVED
  reason no longer applies?    fence retire <id> -m "what changed"
  code lives somewhere new?    fence reanchor <id> <file>:<line>
  commit anyway?               git commit --no-verify
```

Renaming `resp` to `response` instead doesn't block anything. The note follows the code:

```text
$ fence check
[renamed] 1559744b  client.py:4  in Client.fetch
    why: Vendor API returns 200 on auth failure; the error is only in the body.

$ fence update
fence: 1559744b now follows client.py:4  in Client.fetch
```

## Commands

| Command | What it does |
| --- | --- |
| `fence init` | Install the pre-commit hook |
| `fence add FILE:LINE[-END] -m "why"` | Record why a statement exists. `--from-blame` borrows the message of the commit that wrote the line; `--source` records a link or ticket |
| `fence list [PATH]` | List notes |
| `fence check [--staged]` | Find each note's code and report what happened to it. `--staged` is what the hook runs: staged files only |
| `fence update` | Re-pin notes whose code was renamed or moved |
| `fence confirm ID` | The code changed, but the reason still holds |
| `fence reanchor ID FILE:LINE` | Point a note at different code |
| `fence retire ID -m "what changed"` | The reason no longer applies. The note moves to `.fence/retired/`, because why a fence came down is worth remembering too |

IDs can be shortened to any unique prefix.

## How it finds code again

Line numbers break after the first refactor, so a note is pinned to a **statement in the syntax
tree** instead, identified by fingerprints of its structure. Finding it again is a cascade,
strictest test first:

| Result | Meaning | Commit |
| --- | --- | --- |
| `ok` | Same statement (formatting and comments ignored), same scope | passes |
| `renamed` | Same statement once identifiers are renamed consistently | passes |
| `moved` | Same statement in another function, class, or file | passes |
| `changed` | Most similar statement of the same kind, if it's clearly the best match | warns |
| `ambiguous` | One of several identical copies was removed; can't tell which | warns |
| `removed` | None of the above | **blocks** |

Generic statements like `return None` or `if x is None: return` show up everywhere, so `fence`
guards against false comfort:

- A statement is only followed out of its own scope if it's long enough to be distinctive.
- The note records how many identical copies of the statement existed. If that number drops,
  `fence` says so instead of assuming the survivor is the one you meant.
- A fuzzy match only counts if it's more similar than any lookalike that already existed when
  the note was made. A neighbouring guard can't stand in for a deleted one.
- A move has to be a copy appearing somewhere that didn't have one. The note records how many
  copies already existed in other files, so boilerplate that was duplicated all along — a main
  guard, a field default, a log line — can't vouch for code that just disappeared.

Notes are plain JSON files in `.fence/notes/`, committed and reviewed with the code. There's
no server and no account.

## Measured on real code

Against a real 22-file Python project, with 20 notes placed on statements picked at random.

**Replaying its history.** Notes placed at the first commit, then walked forward through every
later commit: 18 stayed `ok`, 2 reported `changed`, and nothing was falsely reported as removed.
Both `changed` verdicts were right — a registry gained two entries, and a constructor gained a
line. Caveat: that history contained no renames or file moves, so it proves little on its own.
Hence the second harness.

**Refactors with a known right answer** (`tools/stress.py`), applied to the same code:

| Change | Must | Result |
| --- | --- | --- |
| Every file rewritten from its AST (all formatting and comments gone) | not alarm | 20/20 still found |
| Identifiers renamed throughout a file | not alarm | 1/1 |
| A class cut and appended to another file | not alarm | 1/1 |
| The noted statement deleted outright | alarm | 20/20 caught |
| A literal inside the noted statement changed | not stay silent | 9/9 flagged |

The first run of that harness failed 5 of those cases: deleting `if __name__ == "__main__":` was
reported as "moved", because an identical line lives in another file. Counting a token or two
more would not have helped — long boilerplate is still boilerplate. That result is what the
copies-elsewhere rule above exists to fix.

**A larger corpus.** The same harness against a 287-file, 156,000-line slice of the Python 3.14
standard library, with 40 notes: every note was still found after all 287 files were rewritten
from their ASTs, 40/40 deletions were caught, 16/16 literal edits were flagged, and there were
0 surprising verdicts.

**Public projects.** The harnesses take any git repository, so the same refactors were applied to
four well-known ones. 3,613 Python files in total:

| Project | Python files | Surprising verdicts |
| --- | --- | --- |
| psf/requests | 37 | 0 |
| pallets/flask | 83 | 0 |
| pytest-dev/pytest | 274 | 0 |
| django/django | 2,932 | 0 |
| Python 3.14 standard library slice | 287 | 0 |

Replaying their histories held every note too: 20 notes placed at the start of a window and walked
forward commit by commit were still `ok` at the end for requests, flask, pytest and django — 24 to
39 commits each — with nothing falsely reported as removed, both with and without re-pinning.
Django's 2,930 files take 107s to walk that way, since only the files a commit touched are re-read.

That run earned its keep immediately: on `psf/requests` a renamed variable was reported as a
deletion, which turned out to be a real bug in both implementations — value names and attribute
names were numbered in one namespace, so renaming variables (which leaves attributes spelled as
they were) shifted every attribute and broke the match. Two lines of pygments style table found
what a hand-written test would not have thought to try. The fix and its conformance case are in
the suite; the schemes it retired are recorded in `docs/FORMAT.md`.

It also made one limitation concrete: a short, generic statement moved to another file reads as
`removed`, not `moved`. `from django.urls import set_script_prefix` looks the same everywhere, so
following it across files would be guessing. Fence errs toward speaking up, and `fence reanchor`
settles it.

**Speed.** That corpus was also slow enough to be worth profiling, which said something useful:
parsing is everything, and locating is free.

| Step | Before | After |
| --- | --- | --- |
| Parse 287 files (94,605 statements) | 26.1s | 8.4s |
| Anchor 40 notes, once files are parsed | ~6s | 1.0s |
| Locate 40 notes against a parsed index | 0.00s | 0.00s |
| The whole stress harness over the corpus | >10 min | 51s |
| Count copies across django's 2,932 files | 32.4s | 1.4s (cache warm) |

Three changes, none of which move a single fingerprint: identifiers are renamed *while* the
statement is serialized rather than on a deep copy of it — copying was over half the cost of
reading a file — copies elsewhere are totalled once for the repository instead of rescanned per
note, and the harness re-reads only the file it just edited. All 94,605 statements were checked
to hash identically before and after, so existing notes keep working.

## Status

This is a prototype of the core loop: anchoring plus the hook.

- **Python only.** It uses the standard library `ast` and `tokenize` modules. Nothing in the design
  is Python-specific; tree-sitter would bring the same anchoring to other languages.
- **Heavily edited short statements** can read as `removed` rather than `changed`. It errs toward
  speaking up; `fence reanchor` fixes it.
- **Notes on large blocks are large**, because the anchor stores the block's token list. A
  MinHash signature would give them a fixed size.
- **The first command on a large repository is slow.** Knowing how many copies of a statement live
  elsewhere means looking at every file, and the first look has to parse them: 32s for django's
  2,932 files. After that the parse cache in `.fence/cache.json` brings it to 1.4s, and only files
  whose content changed are parsed again. Cold is still cold, and the cache costs 9 MB there.
- The hook only checks notes whose files are in the commit, so it never nags about code nobody
  touched.

## Where it's going

1. **Capture reasons for free.** Mine PR descriptions, review threads, and linked issues for
   sentences that explain why code exists, and propose them as notes with a link to the source.
   Nobody writes documentation voluntarily, so the tool should do the writing and ask only for a
   yes or no.
2. **Finish the Rust implementation.** Its anchoring passes the shared conformance cases; the note
   store, git plumbing and hook are not written yet, so `python/` is still the working command.
   A single binary matters because a hook that needs the right Python environment in every clone
   is an adoption tax.
3. **Other languages.** The Rust side already parses with tree-sitter, so a new language is
   mostly a grammar plus its statement and scope rules — and a set of conformance cases.
4. **Make it fast on large repositories**, per Status above.
5. **Surface notes where people are:** a PR check, and an editor hint on hover.

## Layout

| Path | What |
| --- | --- |
| `python/` | The reference implementation: `fence/`, its tests, and the measurement harnesses in `tools/` |
| `rust/` | Second implementation, on tree-sitter. Anchoring only so far — no note store, git plumbing or hook |
| `conformance/cases/*.toml` | Language-neutral cases every implementation must agree on |
| `docs/FORMAT.md` | The note format, and the rules an implementation must honour |

The two implementations sit side by side rather than on separate branches, so one pull request
can change a rule and both implementations together, and CI holds both to the same conformance
cases. They share no code and no fingerprints: one parses with Python's `ast`, the other with
tree-sitter, and their hashes are deliberately different bytes — which is what the anchor's
`version` field records. What they must share is the verdict, and that is all the cases check.

## Development

```bash
cd python
python -m unittest
```

The suite includes end-to-end tests that create real git repos and drive the real pre-commit
hook, plus the shared conformance cases.

Two harnesses measure the anchoring against real code. Both clone or restore what they read and
never write to your repository:

```bash
python tools/replay.py <repo> <clone-dir>   # walk real history, count false alarms
python tools/stress.py <clone-dir>          # apply refactors with a known right answer
python tools/bench.py <repo> --profile      # where the time goes
```

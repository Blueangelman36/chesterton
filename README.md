# chesterton

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
pip install -e chesterton        # or run it in place: python -m fence
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

Notes are plain JSON files in `.fence/notes/`, committed and reviewed with the code. There's
no server and no account.

## Status

This is a prototype of the core loop: anchoring plus the hook.

- **Python only.** It uses the standard library `ast` and `tokenize` modules. Nothing in the design
  is Python-specific; tree-sitter would bring the same anchoring to other languages.
- **Heavily edited short statements** can read as `removed` rather than `changed`. It errs toward
  speaking up; `fence reanchor` fixes it.
- **Notes on large blocks are large**, because the anchor stores the block's token list. A
  MinHash signature would give them a fixed size.
- The hook only checks notes whose files are in the commit, so it never nags about code nobody
  touched.

## Where it's going

1. **Capture reasons for free.** Mine PR descriptions, review threads, and linked issues for
   sentences that explain why code exists, and propose them as notes with a link to the source.
   Nobody writes documentation voluntarily, so the tool should do the writing and ask only for a
   yes or no.
2. **Other languages** via tree-sitter.
3. **Surface notes where people are:** a PR check, and an editor hint on hover.
4. **A single fast binary** (Rust) once the design settles.

## Development

```bash
python -m unittest
```

The suite includes end-to-end tests that create real git repos and drive the real pre-commit hook.

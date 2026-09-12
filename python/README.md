# chesterton

*Don't take down a fence until you know why it was put up.*

`fence` remembers **why** code exists, and speaks up when a commit is about to delete it.

Some of the most painful bugs start the same way: someone removes a strange-looking retry loop,
a `sleep(50)`, or an `if` that "can't happen", and it turns out it could. The person who wrote it
knew why. That reason lived in a closed pull request, a chat thread, or their head, and none of
those are open when the next person edits the file.

Notes are pinned to statements in the syntax tree rather than to line numbers, so they follow the
code through reformatting, identifier renames, and moves between files.

```bash
pip install chesterton
cd your-project
fence init
fence add src/client.py:42 -m "Vendor API returns 200 on auth failure; the error is in the body."
```

Later, when someone deletes that check:

```text
$ git commit -m "Simplify fetch"
[REMOVED] 1559744b  src/client.py:42  in Client.fetch
    why: Vendor API returns 200 on auth failure; the error is in the body.
    was: if resp.json().get("error") == "unauthorized":

fence: 1 note(s) checked: 1 REMOVED
  reason no longer applies?    fence retire <id> -m "what changed"
  code lives somewhere new?    fence reanchor <id> <file>:<line>
  commit anyway?               git commit --no-verify
```

Renaming or moving that code instead blocks nothing — the note follows it.

## For coding agents

```bash
fence why src/client.py --json    # before changing code you did not write
fence check --json                # what a blocked commit is objecting to
fence init --agents               # write the instructions where agents will find them
```

## Status

A prototype of the core loop: anchoring plus the hook. **Python source only** — the design is not
Python-specific, and a tree-sitter implementation for other languages is in progress. Measured
against requests, flask, pytest, django and a slice of the standard library (3,613 files): no
false alarms, and every deliberate deletion caught.

No dependencies. Python 3.11+. MIT licensed.

Full documentation, the note format specification, and the conformance suite live in the
repository.

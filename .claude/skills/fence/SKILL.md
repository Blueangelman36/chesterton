---
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

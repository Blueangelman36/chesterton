//! Text both implementations write into a repository.
//!
//! Kept identical to the Python implementation's copies on purpose: whichever
//! one a project installs, the files it leaves behind should read the same.

pub const FENCE_README: &str = r#"# .fence

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
"#;

pub const AGENTS_MARKER: &str = "<!-- fence -->";

pub const AGENTS_BLOCK: &str = r#"<!-- fence -->
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
"#;

pub const SKILL: &str = r#"---
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
"#;

pub const USAGE: &str = r#"usage: fence <command> [options]

remember why code exists, and speak up before it's removed.

  init [--agents]                     install the pre-commit hook
  why FILE[:LINE[-END]] [--json]      what reasons are recorded for this file or line
  add FILE:LINE[-END] -m "why"        record why a statement exists
      [--from-blame] [--source S] [--also]
  list [PATH]                         list notes, optionally under a path
  statements FILE [--json]            every statement a note could be pinned to
  check [--staged] [--json]           find each note's code and report what happened
  update                              re-pin notes whose code was renamed or moved
  confirm ID                          the code changed, but the reason still holds
  reanchor ID FILE:LINE               point a note at different code
  retire ID -m "what changed"         the reason no longer applies
"#;

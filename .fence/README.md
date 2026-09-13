# .fence

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

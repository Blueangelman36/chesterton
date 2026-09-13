<!-- fence -->
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

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

<!-- asof -->
## Checked numbers (`asof`)

Some numbers in this repository's documents carry a trailing comment like
`<!-- asof:dashboard-port -->` (or `# asof:dashboard-port` in config and code).
Those numbers are claims that CI verifies against wherever the value really
comes from, so they cannot quietly stop being true. `asof.ini` records what each
one means and who or what settles it.

What this asks of you:

- **Changing a marked number is expected.** Keep the `asof:` comment on its line.
- **Before changing a constant in code, ask whether a document states it too.**
  Run `asof check`. If a README and a config file both name the same value, they
  are one claim, and changing one without the other fails the build.
- **Never delete an `asof:` comment to make a check pass.** That switches the
  check off rather than fixing anything. asof remembers which files carried each
  claim, so removing a marker from one of them is reported as `DROPPED`, names
  the file, and fails the build anyway.
- **`asof why NAME`** explains a claim: what it says, who settles it, when it was
  last true, and everywhere it is written. It runs nothing and changes nothing.
- **`asof check --json`** gives machine-readable results as
  `{tool, checked, blocked, counts, items[]}`. Each item leads with `id`,
  `status`, `where` and `why`, and every failing one carries a `remedy` field
  with the next step. `blocked` tells you whether this run should stop a build.
  `list`, `suggest`, `why`, `update` and `touch` answer in the same shape.

If `asof` is not on PATH, look for a vendored `asof.pyz` at the repository root
and run `python asof.pyz check` - it is the whole tool in one file and needs no
install step, no virtualenv and no network.

Here only one number is a claim: the oldest supported Python, stated in both
READMEs, `python/pyproject.toml` and the CI `package` job. Install asof with
`pip install git+https://github.com/Blueangelman36/asof`.
<!-- /asof -->

# The note format

This is the contract between implementations. A note written by one must be
readable by another, and the two must reach the same verdict about the same
code. The conformance suite is what enforces the second half.

## Where notes live

```
.fence/notes/<id>.json      active notes
.fence/retired/<id>.json    notes whose reason no longer applies
.fence/cache.json           derived: fingerprints per file, keyed by content
```

Only the first two belong in a commit, and `.fence/.gitignore` keeps the third
out of one. An implementation may cache however it likes, or not at all; the
cache is not part of this format. Delete it and nothing is lost but time.

Plain files, committed with the code, reviewed in the same pull request. No
server, no database, no account.

## A note

```json
{
  "id": "1559744b",
  "reason": "Vendor API returns 200 on auth failure; the error is only in the body.",
  "source": "commit 4f2a1b9c8d3e",
  "author": "Ada",
  "created": "2026-09-11",
  "snippet": "if resp.json().get(\"error\") == \"unauthorized\":",
  "anchor": { }
}
```

| Field | Meaning |
| --- | --- |
| `id` | Short hex string, unique within the repository. Users may abbreviate it to any unique prefix |
| `reason` | Why the code exists. The whole point of the file |
| `source` | Where the reason came from: a commit, ticket, or URL. May be `null` |
| `author` | Who recorded it. May be `null` |
| `created` | `YYYY-MM-DD` |
| `snippet` | The code as it read when the note was made. Shown to the user, and the fallback for re-pinning a note whose fingerprints an implementation can't compare |
| `anchor` | How to find the code again, below |

A retired note is the same object plus `retired` (a date) and `retired_because`.

## The anchor

```json
{
  "version": 1,
  "path": "src/client.py",
  "scope": "Client.fetch",
  "kind": "If",
  "line": 42,
  "exact": "9f2c1a7b3d5e8104",
  "shape": "be71c4a20d9f3356",
  "dupes": {
    "exact": 1, "shape": 1,
    "other_exact": 0, "other_shape": 0,
    "far_exact": 0, "far_shape": 0
  },
  "rival": 0.34,
  "tokens": ["if", "resp", ".", "json", "(", ")"]
}
```

| Field | Meaning |
| --- | --- |
| `version` | Which implementation's fingerprint scheme produced `exact`, `shape` and `tokens` |
| `path` | Repository-relative, forward slashes |
| `scope` | Dotted names of enclosing functions and classes; `""` at module level |
| `kind` | Syntactic category, used to avoid comparing an `if` against a loop |
| `line` | Where it was last seen. A hint for tie-breaking only, never an identity |
| `exact` | Fingerprint of the statement's structure, ignoring formatting and comments |
| `shape` | Same, with identifiers renamed to a canonical sequence, so a consistent rename still matches |
| `dupes` | Copies of this statement when the note was made: in the same scope, elsewhere in the same file (`other_*`), and in other files (`far_*`) |
| `rival` | Similarity of the most similar other statement of the same kind nearby, when the note was made |
| `tokens` | The statement's tokens, for similarity comparison |

Versions are one registry shared by all implementations, because a note records
the scheme that wrote it, not the program that was running:

| Version | Scheme |
| --- | --- |
| 1 | Python `ast`, first scheme. Retired |
| 2 | tree-sitter, first scheme. Retired |
| 3 | Python `ast`, value names and attribute names numbered separately |
| 4 | tree-sitter, the same |

Schemes 1 and 2 shared one namespace between value names and attribute names.
Renaming a variable in real code leaves attributes spelled as they were, so in a
file where a name is also an attribute — `Error` alongside `Generic.Error`, which
is ordinary in generated or table-driven code — every attribute's number shifted
and an ordinary rename read as a deletion. The conformance suite now carries that
case; it was found by running the stress harness over a real project.

**A note carries one anchor per scheme**, which is how two implementations share
a repository. `anchors` maps a version to its anchor:

```json
"anchors": {
  "3": { "version": 3, "path": "src/client.py", "exact": "9f2c…", "…": "…" },
  "4": { "version": 4, "path": "src/client.py", "exact": "b71a…", "…": "…" }
}
```

Each build reads its own entry, and writing back **replaces only its own**. A note
that has no entry for the reading build gets the `foreign` verdict, and that
build's `fence update` adds its entry beside the other rather than replacing it —
after which both builds find what they wrote and neither keeps re-pinning the
other's work.

A note written before this (a single `anchor` object) is read as a one-entry map
and rewritten in the new shape the next time it is saved.

`version` exists because `exact`, `shape` and `tokens` are **implementation-defined**.
A tree-sitter implementation will hash the same code differently from a Python
`ast` one, and that is fine. An implementation that meets an anchor whose
`version` it does not own MUST NOT compare fingerprints. It should relocate the
statement by similarity, or from `snippet`, and re-pin the note under its own
version. Anything else reports every note in the repository as removed.

## Verdicts

| Verdict | Meaning | Effect on a commit |
| --- | --- | --- |
| `ok` | Same statement, same scope | passes |
| `renamed` | Same statement once identifiers are renamed consistently | passes |
| `moved` | Same statement in another scope or file | passes |
| `changed` | Best similar statement of the same kind | warns |
| `ambiguous` | One of several identical copies is gone; which one is unknowable | warns |
| `foreign` | Written under another scheme, so its fingerprints cannot be compared | warns |
| `removed` | Not found | **blocks** |
| `unparseable` | The file does not currently parse, so nothing was checked | warns |

## Rules an implementation must honour

Search order is strictest first: `exact` in the same scope, then `shape`, then
the same fingerprints in another scope or file, then similarity.

Three rules keep lookalikes from producing false comfort. They exist because
each one failed against real code at some point:

1. **Distinctiveness.** A statement is only followed out of its own scope when
   it is long enough to be distinctive. `return None` is not.
2. **Copy counting.** If fewer copies of the statement exist in its scope than
   `dupes` records, one was deleted, and which one is unknowable: report
   `ambiguous` rather than pointing at a survivor.
3. **A move is an arrival.** Claim `moved` only when copies elsewhere
   *outnumber* what `dupes` recorded. Boilerplate that was duplicated all
   along — a main guard, a field default, a log line — must never vouch for
   code that just disappeared.

A similarity match additionally has to beat `rival`: if nothing is more similar
to the note's code than a lookalike that was already there, the code is gone.

## Conformance

`conformance/cases/*.toml` holds cases every implementation must agree on:

```toml
[[case]]
name = "the statement was deleted"
expect = "removed"          # the verdict
language = "python"         # optional, defaults to python; runners skip what they can't read
anchor_match = "if resp"    # anchor the statement whose first line contains this
expect_file = "auth.py"     # optional, for `moved`
expect_scope = "_check"     # optional, for `moved`

[case.before]               # files as they were when the note was made
"app.py" = '''...'''

[case.after]                # files as they are now
"app.py" = '''...'''
```

A runner builds the anchor from `before`, indexing every file in it so the
copy counts are right, then locates the statement in `after` and compares the
verdict. Cases are the specification; prose in this document that contradicts a
case is wrong.

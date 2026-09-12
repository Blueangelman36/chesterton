"""Structural anchors: pin a note to a statement and find it again after edits.

Line numbers break after the first refactor, so a note is pinned to a statement
in the syntax tree instead. Relocating it is a cascade, strictest test first:

  ok         the same statement (formatting and comments ignored), same scope
  renamed    the same statement once identifiers are renamed consistently
  moved      the same statement in another function, class, or file
  changed    the most similar statement of the same kind, if clearly the best
  removed    none of the above

Generic statements like `return None` appear everywhere, so a statement is only
followed out of its scope when it is long enough to be distinctive, and a fuzzy
match only counts if it beats every lookalike that existed when the note was made.
"""

from __future__ import annotations

import ast
import bisect
import hashlib
from collections import Counter
import io
import textwrap
import tokenize
from dataclasses import dataclass
from functools import cached_property
from typing import Protocol

from . import FenceError

SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
SKIP_TOKENS = {tokenize.NEWLINE, tokenize.NL, tokenize.INDENT, tokenize.DEDENT,
               tokenize.COMMENT, tokenize.ENDMARKER, tokenize.ENCODING}
# Fields that vary between Python versions or carry no meaning for identity.
SKIP_FIELDS = {"ctx", "type_comment", "kind"}
# The string fields holding a name, and which namespace that name lives in.
# Values and attributes are numbered separately because renaming a variable
# leaves attributes spelled as they were: where a name is also an attribute
# (`Error` alongside `Generic.Error`), one shared namespace shifted every
# attribute's number and made an ordinary rename look like a deletion.
IDENT_FIELDS = {("Name", "id"): "value", ("arg", "arg"): "value",
                ("FunctionDef", "name"): "value", ("AsyncFunctionDef", "name"): "value",
                ("ClassDef", "name"): "value", ("Attribute", "attr"): "attr"}

# Fingerprints are hashes of a Python-specific structure, so a different
# implementation (tree-sitter, another language) will hash the same code
# differently. Notes record the version that made them; older ones skip the hash
# comparisons and are found by similarity instead, so `fence update` can re-pin
# them rather than reporting every one as removed.
ANCHOR_VERSION = 3

MIN_DISTINCTIVE_TOKENS = 12
CHANGED_THRESHOLD = 0.5
SNIPPET_LINES = 12


class Unparseable(FenceError):
    pass


class Reader(Protocol):
    def read(self, path: str) -> str | None: ...
    def paths(self) -> list[str]: ...


@dataclass
class Candidate:
    """A statement in the current code that a note might be pinned to."""
    path: str
    scope: str
    kind: str
    line: int
    end_line: int
    exact: str
    shape: str
    tokens: list[str]
    snippet: str

    @cached_property
    def features(self) -> set:
        return features(self.tokens)


@dataclass
class Match:
    how: str
    candidate: Candidate | None = None
    similarity: float = 1.0


def candidates(path: str, source: str) -> list[Candidate]:
    """Every statement in a file, outermost first, with its fingerprints."""
    try:
        tree = ast.parse(source)
        toks = _tokens(source)
    except (SyntaxError, ValueError, tokenize.TokenError) as e:
        raise Unparseable(f"{path} doesn't parse: {e}") from e
    lines = source.split("\n")
    starts = [start for start, _ in toks]
    out = []

    def walk(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                out.append(_candidate(path, child, scope, lines, toks, starts))
            inner = scope
            if isinstance(child, SCOPES):
                inner = f"{scope}.{child.name}" if scope else child.name
            walk(child, inner)

    walk(tree, "")
    return out


def pick(cands: list[Candidate], start: int, end: int) -> Candidate | None:
    """The smallest statement covering the lines; the outermost one on a tie."""
    covering = [(c.end_line - c.line, i, c) for i, c in enumerate(cands)
                if c.line <= start and c.end_line >= end]
    return min(covering)[2] if covering else None


def far_copies(target: Candidate, elsewhere) -> tuple[int, int]:
    """Copies of a statement among candidates from other files, counted the slow way.

    For callers holding a plain list; anything with an Index should ask it
    instead, since it totals the whole repository once.
    """
    exact = shape = 0
    for candidate in elsewhere:
        exact += candidate.exact == target.exact
        shape += candidate.shape == target.shape
    return exact, shape


def make_anchor(target: Candidate, siblings: list[Candidate], far: tuple[int, int] = (0, 0)) -> dict:
    """Everything needed to find `target` again, including how crowded its neighborhood is.

    `far` is how many copies of the statement already live in other files. That
    count is what later tells a real move apart from boilerplate that was
    duplicated all along.
    """
    same_scope = [c for c in siblings if c.scope == target.scope]
    other_scope = [c for c in siblings if c.scope != target.scope]
    # The statement is not its own lookalike, even when `siblings` came from a
    # separate parse of the same file and holds a different object for it.
    lookalikes = [c for c in _near(siblings, target.scope, _distinctive(target.tokens))
                  if not _same_statement(c, target) and c.kind == target.kind]

    def copies(pool, key):
        return sum(getattr(c, key) == getattr(target, key) for c in pool)

    return {
        "version": ANCHOR_VERSION,
        "path": target.path,
        "scope": target.scope,
        "kind": target.kind,
        "line": target.line,
        "exact": target.exact,
        "shape": target.shape,
        "dupes": {
            "exact": copies(same_scope, "exact"),
            "shape": copies(same_scope, "shape"),
            "other_exact": copies(other_scope, "exact"),
            "other_shape": copies(other_scope, "shape"),
            "far_exact": far[0],
            "far_shape": far[1],
        },
        "rival": max((similarity(target.features, c.features) for c in lookalikes), default=0.0),
        "tokens": target.tokens,
    }


class Index:
    """Statements of every Python file a reader can see, parsed on demand."""

    def __init__(self, reader: Reader, cache=None):
        self.reader = reader
        self.cache = cache
        self.broken: set[str] = set()
        self._files: dict[str, list[Candidate]] = {}
        self._tallied: tuple | None = None

    def get(self, path: str) -> list[Candidate]:
        if path not in self._files:
            source = self.reader.read(path)
            cands = []
            if source is not None:
                try:
                    cands = candidates(path, source)
                except Unparseable:
                    self.broken.add(path)
            self._files[path] = cands
        return self._files[path]

    def invalidate(self, path: str) -> None:
        """Forget one file, for callers that edit a file and look again."""
        self._files.pop(path, None)
        self.broken.discard(path)
        if self._tallied is not None:
            self._retally(path)

    def copies_elsewhere(self, path: str, exact: str, shape: str) -> tuple[int, int]:
        """How many copies of these fingerprints live in other files."""
        totals, per_file, _ = self._tally()
        mine = per_file.get(path, (Counter(), Counter()))
        return totals[0][exact] - mine[0][exact], totals[1][shape] - mine[1][shape]

    def find_elsewhere(self, path: str, fingerprint: str, key: str) -> list[Candidate]:
        """Statements in other files carrying this fingerprint.

        Only the files the tally says can contain it are parsed, so looking for
        code that moved costs one file rather than the whole repository.
        """
        _, _, where = self._tally()
        slot = 0 if key == "exact" else 1
        found = []
        for other in where[slot].get(fingerprint, ()):
            if other != path:
                found.extend(c for c in self.get(other) if getattr(c, key) == fingerprint)
        return found

    def _tally(self) -> tuple:
        """Fingerprint counts for every file, and which files hold each fingerprint.

        The one place that looks at the whole repository, so it is also where the
        parse cache is filled and written.
        """
        if self._tallied is None:
            totals: tuple[Counter, Counter] = (Counter(), Counter())
            per_file: dict[str, tuple[Counter, Counter]] = {}
            where: tuple[dict, dict] = ({}, {})
            for other in self.reader.paths():
                if not other.endswith(".py"):
                    continue
                counts = self._counts_for(other)
                if counts is None:
                    continue
                per_file[other] = counts
                for slot in (0, 1):
                    totals[slot].update(counts[slot])
                    for fingerprint in counts[slot]:
                        where[slot].setdefault(fingerprint, []).append(other)
            if self.cache is not None:
                self.cache.save(keep=per_file)
            self._tallied = (totals, per_file, where)
        return self._tallied

    def _counts_for(self, path: str) -> tuple[Counter, Counter] | None:
        if self.cache is None:
            return _count(self.get(path))  # no point reading bytes nothing will check
        source = self.reader.read(path)
        if source is None:
            return None
        known = self.cache.get(path, source)
        if known is not None:
            return known
        counts = _count(self.get(path))
        self.cache.put(path, source, counts)
        return counts

    def _retally(self, path: str) -> None:
        """Swap one file's counts in place, so editing a file doesn't cost a full tally."""
        totals, per_file, where = self._tallied
        previous = per_file.pop(path, None)
        if previous is not None:
            for slot in (0, 1):
                totals[slot].subtract(previous[slot])
                for fingerprint in previous[slot]:
                    holders = where[slot].get(fingerprint)
                    if holders and path in holders:
                        holders.remove(path)
        current = self._counts_for(path)
        if current is None:
            return
        per_file[path] = current
        for slot in (0, 1):
            totals[slot].update(current[slot])
            for fingerprint in current[slot]:
                where[slot].setdefault(fingerprint, []).append(path)


def locate(anchor: dict, index: Index) -> Match:
    """Find the statement an anchor was made from, in the code as it is now."""
    path, scope, line = anchor["path"], anchor["scope"], anchor["line"]
    home = index.get(path)
    if path in index.broken:
        return Match("unparseable")
    distinctive = _distinctive(anchor["tokens"])
    near = _near(home, scope, distinctive)
    dupes = anchor.get("dupes", {})

    def closest(cands):
        return min(cands, key=lambda c: (c.path != path, c.scope != scope, abs(c.line - line)))

    # Same code in this file, possibly with renamed identifiers or a new home.
    # If there are fewer identical copies than when the note was made, one of
    # them was deleted and we can't vouch for this one.
    if anchor.get("version") == ANCHOR_VERSION:
        for key, how in (("exact", "ok"), ("shape", "renamed")):
            hits = [c for c in near if getattr(c, key) == anchor[key]]
            here = [c for c in hits if c.scope == scope]
            if here and len(here) >= dupes.get(key, 1):
                return Match(how, closest(here))
            if here and key == "exact":
                return Match("ambiguous", closest(here))
            if hits and not here and len(hits) > dupes.get(f"other_{key}", 0):
                return Match("moved", closest(hits))

        # A move means a copy turned up somewhere that didn't have one before.
        # Boilerplate that was always duplicated elsewhere -- a main guard, a
        # field default, a log line -- is not the statement this note was about,
        # so it can't vouch for code that just disappeared.
        if distinctive:
            far = index.copies_elsewhere(path, anchor["exact"], anchor["shape"])
            for key, count in (("exact", far[0]), ("shape", far[1])):
                if count > dupes.get(f"far_{key}", 0):
                    hits = index.find_elsewhere(path, anchor[key], key)
                    if hits:
                        return Match("moved", closest(hits))

    # Edited in place: the most similar statement of the same kind, as long as
    # it's more similar than any lookalike that was already there.
    target = features(anchor["tokens"])
    scored = [(similarity(target, c.features), c) for c in near if c.kind == anchor["kind"]]
    if scored:
        sim, best = max(scored, key=lambda sc: (sc[0] + (0.1 if sc[1].scope == scope else 0.0),
                                                -abs(sc[1].line - line)))
        if sim >= CHANGED_THRESHOLD and sim > anchor.get("rival", 0.0):
            return Match("changed", best, sim)
    return Match("removed")


def scope_label(scope: str) -> str:
    return scope or "<module>"


def features(tokens: list[str]) -> set:
    """Tokens plus adjacent pairs: order-aware, but tolerant of small edits."""
    grams = set(tokens)
    grams.update(zip(tokens, tokens[1:]))
    return grams


def similarity(a: set, b: set) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 1.0


def _count(found: list[Candidate]) -> tuple[Counter, Counter]:
    return Counter(c.exact for c in found), Counter(c.shape for c in found)


def _same_statement(a, b):
    return a is b or (a.path == b.path and a.line == b.line and a.exact == b.exact)


def _near(cands, scope, distinctive):
    return [c for c in cands if c.scope == scope or distinctive]


def _distinctive(tokens):
    return len(tokens) >= MIN_DISTINCTIVE_TOKENS


def _candidate(path, node, scope, lines, toks, starts):
    first = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    col = _char_col(lines, first, node.col_offset) if first == node.lineno else 0
    end_col = _char_col(lines, node.end_lineno, node.end_col_offset)
    lo = bisect.bisect_left(starts, (first, col))
    hi = bisect.bisect_left(starts, (node.end_lineno, end_col))
    exact, shape = _fingerprints(node)
    return Candidate(
        path=path,
        scope=scope,
        kind=_kind(node),
        line=first,
        end_line=node.end_lineno,
        exact=_hash(exact),
        shape=_hash(shape),
        tokens=[text for _, text in toks[lo:hi]],
        snippet=_snippet(lines, first, node.end_lineno),
    )


def _kind(node):
    name = type(node).__name__
    if isinstance(node, ast.Expr):
        name += "." + type(node.value).__name__
    return name


def _fingerprints(node) -> tuple[str, str]:
    """Render a statement twice in one walk: as written, and identifier-blind.

    Like ast.dump, but skipping empty and bookkeeping fields so the rendering is
    stable across Python versions. Renaming as we go, rather than renaming a copy
    of the tree, is worth the small amount of bookkeeping: deep-copying every
    statement was over half the cost of reading a file. It is safe because
    ast.iter_fields yields each identifier field in the same order a walk of the
    tree would have reached it, so the numbering comes out the same.
    """
    namespaces: dict[str, dict[str, str]] = {"value": {}, "attr": {}}
    exact: list[str] = []
    shape: list[str] = []
    # Hot loop: a statement is rendered once per enclosing statement, so this
    # runs millions of times on a large repository. Hence no helper calls, and
    # _fields directly instead of ast.iter_fields.
    add_exact = exact.append
    add_shape = shape.append

    def walk(value, identifier=None):
        if isinstance(value, ast.AST):
            kind = type(value).__name__
            add_exact(kind + "(")
            add_shape(kind + "(")
            first = True
            for field in value._fields:
                if field in SKIP_FIELDS:
                    continue
                child = getattr(value, field, None)
                if child is None or child == []:
                    continue
                if first:
                    first = False
                else:
                    add_exact(", ")
                    add_shape(", ")
                add_exact(field + "=")
                add_shape(field + "=")
                walk(child, IDENT_FIELDS.get((kind, field)))
            add_exact(")")
            add_shape(")")
        elif isinstance(value, list):
            add_exact("[")
            add_shape("[")
            for position, item in enumerate(value):
                if position:
                    add_exact(", ")
                    add_shape(", ")
                walk(item, identifier)
            add_exact("]")
            add_shape("]")
        else:
            exact.append(repr(value))
            if identifier and isinstance(value, str):
                seen = namespaces[identifier]
                prefix = "v" if identifier == "value" else "a"
                shape.append(repr(seen.setdefault(value, f"{prefix}{len(seen)}")))
            else:
                shape.append(repr(value))

    walk(node)
    return "".join(exact), "".join(shape)


def _tokens(source):
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type in SKIP_TOKENS or not tok.string:
            continue
        text = tok.string
        if len(text) > 32:  # docstrings and long literals: keep notes small
            text = f"<{_hash(text)[:8]}>"
        out.append((tok.start, text))
    return out


def _char_col(lines, lineno, col):
    """ast columns are UTF-8 byte offsets; tokenize columns are characters."""
    line = lines[lineno - 1] if lineno <= len(lines) else ""
    return col if line.isascii() else len(line.encode()[:col].decode("utf-8", "ignore"))


def _snippet(lines, first, last):
    body = [line.rstrip("\r") for line in lines[first - 1:last]]
    text = textwrap.dedent("\n".join(body[:SNIPPET_LINES]))
    return text + "\n..." if len(body) > SNIPPET_LINES else text


def _hash(text):
    return hashlib.sha1(text.encode()).hexdigest()[:16]

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
import copy
import hashlib
import io
import textwrap
import tokenize
from dataclasses import dataclass
from functools import cached_property
from typing import Iterator, Protocol

from . import FenceError

SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
SKIP_TOKENS = {tokenize.NEWLINE, tokenize.NL, tokenize.INDENT, tokenize.DEDENT,
               tokenize.COMMENT, tokenize.ENDMARKER, tokenize.ENCODING}
# Fields that vary between Python versions or carry no meaning for identity.
SKIP_FIELDS = {"ctx", "type_comment", "kind"}

# Fingerprints are hashes of a Python-specific structure, so a different
# implementation (tree-sitter, another language) will hash the same code
# differently. Notes record the version that made them; older ones skip the hash
# comparisons and are found by similarity instead, so `fence update` can re-pin
# them rather than reporting every one as removed.
ANCHOR_VERSION = 1

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


def make_anchor(target: Candidate, siblings: list[Candidate]) -> dict:
    """Everything needed to find `target` again, including how crowded its neighborhood is."""
    same_scope = [c for c in siblings if c.scope == target.scope]
    lookalikes = [c for c in _near(siblings, target.scope, _distinctive(target.tokens))
                  if c is not target and c.kind == target.kind]
    return {
        "version": ANCHOR_VERSION,
        "path": target.path,
        "scope": target.scope,
        "kind": target.kind,
        "line": target.line,
        "exact": target.exact,
        "shape": target.shape,
        "dupes": {"exact": sum(c.exact == target.exact for c in same_scope),
                  "shape": sum(c.shape == target.shape for c in same_scope)},
        "rival": max((similarity(target.features, c.features) for c in lookalikes), default=0.0),
        "tokens": target.tokens,
    }


class Index:
    """Statements of every Python file a reader can see, parsed on demand."""

    def __init__(self, reader: Reader):
        self.reader = reader
        self.broken: set[str] = set()
        self._files: dict[str, list[Candidate]] = {}

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

    def others(self, path: str) -> Iterator[Candidate]:
        for p in self.reader.paths():
            if p != path and p.endswith(".py"):
                yield from self.get(p)


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
            if hits and not here:
                return Match("moved", closest(hits))

        if distinctive:
            far = list(index.others(path))
            for key in ("exact", "shape"):
                hits = [c for c in far if getattr(c, key) == anchor[key]]
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
    shape = _Canonicalize().visit(copy.deepcopy(node))
    return Candidate(
        path=path,
        scope=scope,
        kind=_kind(node),
        line=first,
        end_line=node.end_lineno,
        exact=_hash(_serialize(node)),
        shape=_hash(_serialize(shape)),
        tokens=[text for _, text in toks[lo:hi]],
        snippet=_snippet(lines, first, node.end_lineno),
    )


def _kind(node):
    name = type(node).__name__
    if isinstance(node, ast.Expr):
        name += "." + type(node.value).__name__
    return name


def _serialize(node) -> str:
    """Like ast.dump, but stable across Python versions: skips empty and bookkeeping fields."""
    if isinstance(node, ast.AST):
        fields = [f"{name}={_serialize(value)}" for name, value in ast.iter_fields(node)
                  if name not in SKIP_FIELDS and value is not None and value != []]
        return f"{type(node).__name__}({', '.join(fields)})"
    if isinstance(node, list):
        return "[" + ", ".join(_serialize(v) for v in node) + "]"
    return repr(node)


class _Canonicalize(ast.NodeTransformer):
    """Rename identifiers to v0, v1, ... in order of first appearance."""

    def __init__(self):
        self.names = {}

    def _canon(self, name):
        return self.names.setdefault(name, f"v{len(self.names)}")

    def visit_Name(self, node):
        node.id = self._canon(node.id)
        return node

    def visit_arg(self, node):
        node.arg = self._canon(node.arg)
        self.generic_visit(node)
        return node

    def visit_Attribute(self, node):
        self.generic_visit(node)
        node.attr = self._canon(node.attr)
        return node

    def _visit_def(self, node):
        node.name = self._canon(node.name)
        self.generic_visit(node)
        return node

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _visit_def


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

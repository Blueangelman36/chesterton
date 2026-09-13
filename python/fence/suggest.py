"""Which statements have a reason nobody wrote down?

Adopting fence on an existing repository means answering that, and a tool cannot
know. What it can do is count the signals that tend to sit beside a reason -- a
comment that explains rather than describes, a timing primitive, a swallowed
error, a number somebody chose -- and put the likeliest first.

The ranking counts; it does not judge. That is deliberate, and borrowed from
asof, which ranks numeric claims by how often the same number is written in two
places rather than by how important it looks. A count can be argued with.

The discount matters as much as the signal: a statement written the same way in
several other files is a convention, not a decision, and convention needs no
reason recorded. It is the same shape as asof discounting round numbers, for the
same reason -- without it, the list fills with things nobody chose.

When there is a comment that explains, it is offered as a draft reason to edit,
not a reason to accept. When there is not, the suggestion is `--from-blame`,
because the commit that wrote the line usually said why.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import anchor as A

# Words that appear when somebody is explaining rather than describing.
EXPLAINS = re.compile(
    r"\b(because|why|so that|otherwise|must|needs? to|has to|workaround|deliberate|deliberately|"
    r"on purpose|intentional|intentionally|do not remove|don't remove|keep|note|caveat|"
    r"vendor|upstream|third.?party|bug|issue|ticket|quirk|breaks?|broken|fails?|failing|"
    r"regression|rate.?limit|race|deadlock|legacy|historical|temporar|until|revert)\b",
    re.IGNORECASE,
)

# A statement that names a browser, platform or wire format is answering to
# something outside this repository, and outside things are never obvious.
ENVIRONMENT = re.compile(
    r"\b(safari|firefox|chrome|chromium|webkit|edge|ie\d*|ios|android|windows|macos|darwin|"
    r"linux|posix|cygwin|msvc|python2|crlf|timezone|dst|proxy|cors|tls|ssl)\b",
    re.IGNORECASE,
)

# Timing is where workarounds hide: nobody sleeps for fifty milliseconds by choice.
TIMING = re.compile(
    r"\b(sleep|timeout|timed_?out|delay|wait|retry|retries|backoff|debounce|throttle|poll|"
    r"polling|setTimeout|setInterval|deadline|expires?)\b",
    re.IGNORECASE,
)

NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])")


def chosen_number(text: str) -> bool:
    """Did somebody pick this number, or is it structure?

    0.05 and 250 and 8420 are decisions. 0, 1, 2 and their decimal spellings are
    how code is shaped -- a dataclass default of 1.0 is not a claim about the world.
    """
    for found in NUMBER.finditer(text):
        token = found.group()
        if "." in token:
            if float(token) not in (0.0, 1.0, 2.0):
                return True
        elif len(token) >= 3:
            return True
    return False

CATCHES = {"Try", "TryStar"}
MERE_PLUMBING = {"Import", "ImportFrom", "Expr.Constant", "Pass", "Global", "Nonlocal"}
TEST_PATH = re.compile(r"(^|/)(tests?|spec)/|(^|/)test_[^/]*$|[._](test|spec)\.[a-z]+$", re.I)

# Above this many copies elsewhere, a statement is how this project writes things
# rather than a decision anybody made.
CONVENTION = 3
# Generous, because a draft that starts mid-sentence is worse than a long one:
# a comment block is one thought, and cutting it from the bottom ruins it.
COMMENT_LINES = 12


@dataclass
class Suggestion:
    path: str
    line: int
    end_line: int
    kind: str
    scope: str
    text: str
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    draft: str | None = None

    @property
    def command(self) -> str:
        where = f"{self.path}:{self.line}"
        if self.draft:
            # A quote inside the draft would end the argument early.
            return f'fence add {where} -m "{self.draft.replace(chr(34), chr(39))}"'
        return f"fence add {where} --from-blame"


STRINGS = re.compile(r"('''|\"\"\")[\s\S]*?\1|\"[^\"\n]*\"|'[^'\n]*'")


def own_text(candidate: A.Candidate, siblings: list[A.Candidate], lines: list[str]) -> str:
    """A statement's own lines, minus anything nested inside it, minus string bodies.

    Two mistakes this avoids, both found by running the command on real code. A
    compound statement otherwise inherits every signal in its body, so `def run`
    outranks the `time.sleep` three lines inside it and the useful suggestion is
    buried. And a signal word inside a string literal -- `encoding="utf-8"`, or a
    test fixture quoting code -- is not the code saying anything.
    """
    inside = set()
    for other in siblings:
        if other.line > candidate.line and other.end_line <= candidate.end_line:
            inside.update(range(other.line, other.end_line + 1))
    kept = [lines[number - 1] for number in range(candidate.line, candidate.end_line + 1)
            if number not in inside and number <= len(lines)]
    # Comments belong to the statement they sit above, not to whatever encloses
    # them: a remark about a sleep should not be credited to the function.
    code = STRINGS.sub("", "\n".join(kept))
    return "\n".join(re.sub(r"#.*$", "", line) for line in code.split("\n"))


def comment_above(lines: list[str], line: int) -> str:
    """The contiguous comment block directly above a statement, if there is one."""
    collected = []
    position = line - 2
    while position >= 0 and len(collected) < COMMENT_LINES:
        text = lines[position].strip()
        if not text.startswith("#"):
            break
        collected.append(text.lstrip("#").strip())
        position -= 1
    return " ".join(reversed(collected)).strip()


def trailing_comment(line: str) -> str:
    """`time.sleep(0.05)  # safari fires focus twice` -- a reason lives here too."""
    code = STRINGS.sub("", line)
    marker = code.find("#")
    return code[marker + 1:].strip() if marker >= 0 else ""


def comment_for(lines: list[str], line: int) -> str:
    """Everything a person wrote about this statement without being asked."""
    above = comment_above(lines, line)
    trailing = trailing_comment(lines[line - 1]) if line - 1 < len(lines) else ""
    return " ".join(part for part in (above, trailing) if part)


def rank(candidate: A.Candidate, body: str, comment: str, copies_elsewhere: int,
         neighbouring: bool = False) -> Suggestion:
    """Score one statement. Every clause is a count, not an opinion.

    `body` is the statement's own text, from own_text: what it says itself rather
    than what anything inside it says.
    """
    found = Suggestion(
        path=candidate.path,
        line=candidate.line,
        end_line=candidate.end_line,
        kind=candidate.kind,
        scope=A.scope_label(candidate.scope),
        text=candidate.snippet.strip().splitlines()[0] if candidate.snippet.strip() else "",
    )

    if comment and EXPLAINS.search(comment):
        found.score += 5
        found.reasons.append("a comment above it explains something, and a comment is deleted "
                             "with the code it explains")
        found.draft = comment
    elif comment:
        found.score += 1
        found.reasons.append("a comment above it, though it only says what the code says")

    if TIMING.search(body):
        found.score += 3
        found.reasons.append("names a timing primitive, and nobody picks a delay by choice")
    environmental = bool(comment and ENVIRONMENT.search(comment))
    if ENVIRONMENT.search(body) or environmental:
        found.score += 3
        found.reasons.append("names a browser, platform or wire format, so it answers to "
                             "something outside this repository")
        # Naming an environment is not explaining, but it is still the best draft
        # available, and a draft is somewhere to start rather than an answer.
        if environmental and not found.draft:
            found.draft = comment
    if candidate.kind in CATCHES:
        found.score += 2
        found.reasons.append("catches something, and which failure it forgives is rarely obvious")
    if chosen_number(body):
        found.score += 2
        found.reasons.append("contains a number somebody chose")

    if copies_elsewhere >= CONVENTION:
        found.score -= 4
        found.reasons.append(f"written the same way in {copies_elsewhere} other places, so it is "
                             "a convention rather than a decision")
    if neighbouring:
        found.score -= 3
        found.reasons.append("a reason is already recorded for the statement just inside or "
                             "around this one")
    if TEST_PATH.search(candidate.path):
        found.score -= 2
        found.reasons.append("a test, where a value is often arbitrary on purpose")
    if candidate.kind in MERE_PLUMBING:
        found.score -= 3
    return found


def collect(index: A.Index, paths: list[str], taken: set[tuple[str, int]],
            limit: int) -> list[Suggestion]:
    """Rank every statement nobody has recorded a reason for yet."""
    found = []
    for path in paths:
        candidates = index.get(path)
        if not candidates:
            continue
        source = index.reader.read(path)
        lines = source.split("\n") if source else []
        here = [span for span in taken if span[0] == path]
        for candidate in candidates:
            span = (candidate.line, candidate.end_line)
            if any((start, end) == span for _, start, end in here):
                continue
            # A reason recorded on the statement inside this one is the same
            # reason. Discounted rather than hidden, in case this one has its own.
            neighbouring = any(candidate.line <= start and candidate.end_line >= end
                               or start <= candidate.line and end >= candidate.end_line
                               for _, start, end in here)
            elsewhere, _ = index.copies_elsewhere(path, candidate.exact, candidate.shape)
            scored = rank(candidate, own_text(candidate, candidates, lines),
                          comment_for(lines, candidate.line), elsewhere, neighbouring)
            if scored.score > 0:
                found.append(scored)
    found.sort(key=lambda s: (-s.score, s.path, s.line))
    return found[:limit]

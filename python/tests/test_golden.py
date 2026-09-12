"""Fingerprints must not move between Python versions.

A note is written under one interpreter and checked under another, often by a
different person on a different machine. `ast` gains fields between versions --
3.12 added type parameters to every definition, for instance -- and if the
fingerprints moved with it, every note in a repository would read as changed on
the day someone upgraded.

So the hashes below are pinned. They are generated from one snippet of deliberately
varied syntax and compared on every supported Python. A failure here means the
scheme is not stable across versions, which is a problem to understand before
regenerating: the notes people have already written are the thing at stake.

    python -m tests.test_golden --regenerate    # only after understanding why

Tokens are deliberately not pinned. The tokenizer legitimately changes between
versions -- 3.12 split f-strings into several tokens -- and tokens only feed the
similarity fallback, so a difference there costs precision, not correctness.
"""

import json
import sys
import unittest
from pathlib import Path

from fence import anchor as A

GOLDEN = Path(__file__).resolve().parent / "golden_fingerprints.json"

SOURCE = '''\
"""Module docstring."""

import time
from typing import Any

CONSTANTS = {"a": 1, "b": None, "c": (1.5, 2j), "d": b"bytes", "e": ...}


@decorator(option=True)
class Client(Base, metaclass=Meta):
    """A class with a docstring, decorators and keywords."""

    retries: int = 3

    def __init__(self, name: str, *args: Any, timeout=1.5, **options) -> None:
        self.name = name
        self.session = None

    async def fetch(self, url, /, *, verify=True):
        async with self.session.get(url) as response:
            if response.json().get("error") == "unauthorized":
                raise AuthError(url)
            return [item.strip() for item in response.lines if item]

    def classify(self, code):
        match code:
            case 200 | 201:
                return "ok"
            case int() as other if other >= 500:
                return f"server {other}"
            case _:
                return None


def retry(attempts=3):
    total = 0
    while (total := total + 1) < attempts:
        try:
            time.sleep(0.05)
        except* TimeoutError:
            continue
        finally:
            del total
    else:
        pass
    lam = lambda x, y=2: x + y
    global _state
    return lam


def focus(element):
    try:
        time.sleep(0.05)
    except AttributeError as error:
        raise RuntimeError("nothing to focus") from error
    else:
        element.focus()
    finally:
        element.settle()
'''


def fingerprints():
    """Every statement's fingerprints, keyed so a failure names the statement."""
    return {f"{c.line}:{c.kind}:{c.scope or '<module>'}": [c.exact, c.shape]
            for c in A.candidates("golden.py", SOURCE)}


class GoldenTest(unittest.TestCase):
    def test_fingerprints_match_the_recorded_scheme(self):
        expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
        actual = fingerprints()
        self.assertEqual(
            set(expected), set(actual),
            "the set of statements changed, so this Python parses the snippet differently")
        for statement, pinned in expected.items():
            with self.subTest(statement):
                self.assertEqual(actual[statement], pinned)

    def test_the_snippet_still_exercises_a_range_of_syntax(self):
        kinds = {c.kind for c in A.candidates("golden.py", SOURCE)}
        for kind in ("ClassDef", "FunctionDef", "AsyncFunctionDef", "Match", "Try", "TryStar",
                     "While", "AnnAssign", "Delete", "Global"):
            self.assertIn(kind, kinds)


if __name__ == "__main__":
    if "--regenerate" in sys.argv:
        GOLDEN.write_text(json.dumps(fingerprints(), indent=2) + "\n", encoding="utf-8")
        print(f"wrote {GOLDEN} from Python {sys.version.split()[0]}")
    else:
        unittest.main()

"""Where does the time actually go? Times the work a command really performs.

Usage:  python tools/bench.py <repo> [--notes 40] [--profile]
"""

import argparse
import cProfile
import pstats
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fence import anchor as A
from fence.cache import Cache
from fence.gitutil import WorktreeReader
from replay import py_files, sample_notes


def timed(label, fn):
    start = time.perf_counter()
    result = fn()
    print(f"  {label:<44}{time.perf_counter() - start:7.2f}s")
    return result


def parsed_index(reader, paths):
    index = A.Index(reader)
    for path in paths:
        index.get(path)
    return index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo")
    parser.add_argument("--notes", type=int, default=40)
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args()

    root = Path(args.repo)
    reader = WorktreeReader(root)
    paths = py_files(root)
    print(f"{len(paths)} python files in {root}")

    index = timed("parse every file", lambda: parsed_index(reader, paths))
    statements = sum(len(index.get(p)) for p in paths)
    print(f"  {'':<44}{statements} statements")

    notes = timed(f"place {args.notes} notes (make_anchor each)",
                  lambda: sample_notes(root, args.notes, 0))

    warm = parsed_index(reader, paths)
    timed(f"locate {len(notes)} notes (files already parsed)",
          lambda: [A.locate(n["anchor"], warm) for n in notes])

    # What `fence add` pays: counting copies of a statement across the repository.
    sample = notes[0]["anchor"]

    def tally(cache):
        index = A.Index(WorktreeReader(root), cache)
        return index.copies_elsewhere(sample["path"], sample["exact"], sample["shape"])

    timed("count copies elsewhere, no cache", lambda: tally(None))
    timed("count copies elsewhere, cold cache", lambda: tally(Cache(root, A.ANCHOR_VERSION)))
    timed("count copies elsewhere, warm cache", lambda: tally(Cache(root, A.ANCHOR_VERSION)))

    if args.profile:
        profiler = cProfile.Profile()
        profiler.enable()
        fresh = parsed_index(reader, paths)
        [A.locate(n["anchor"], fresh) for n in notes]
        profiler.disable()
        print()
        pstats.Stats(profiler).sort_stats("tottime").print_stats(14)


if __name__ == "__main__":
    main()

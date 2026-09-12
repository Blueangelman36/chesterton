"""A parse cache, so recording a note doesn't have to read the whole repository.

Knowing how many copies of a statement live elsewhere means looking at every
file. Parsing them takes tens of seconds on a large project; reading their bytes
and hashing them takes a fraction of a second. So a file's fingerprints are kept
against the hash of its contents, and only files that actually changed are parsed
again.

Derived data: delete it and the next command builds it back.
"""

import hashlib
import json
from collections import Counter
from pathlib import Path

Counts = tuple[Counter, Counter]


class Cache:
    """Read-only is for the hook: a commit should not be writing files, and the
    hook reads staged content, whose file listing is not the worktree's."""

    def __init__(self, root: Path, version: int, writable: bool = True):
        self.dir = root / ".fence"
        self.path = self.dir / "cache.json"
        self.version = version
        self.writable = writable
        self._entries: dict[str, dict] = {}
        self._dirty = False
        self._read()

    def _read(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return  # missing or malformed: it is only a cache
        # Fingerprints from a different scheme are not comparable with these, so
        # a version change throws the whole thing away.
        if isinstance(data, dict) and data.get("version") == self.version:
            files = data.get("files")
            if isinstance(files, dict):
                self._entries = files

    def get(self, path: str, source: str) -> Counts | None:
        entry = self._entries.get(path)
        if entry is None or entry.get("sha") != _sha(source):
            return None
        try:
            return Counter(entry["exact"]), Counter(entry["shape"])
        except (KeyError, TypeError):
            return None

    def put(self, path: str, source: str, counts: Counts) -> None:
        if not self.writable:
            return
        self._entries[path] = {"sha": _sha(source),
                               "exact": dict(counts[0]),
                               "shape": dict(counts[1])}
        self._dirty = True

    def save(self, keep=None) -> None:
        """Write the cache out, forgetting files that are no longer there."""
        if not self.writable:
            return
        if keep is not None:
            gone = set(self._entries) - set(keep)
            for path in gone:
                del self._entries[path]
            self._dirty = self._dirty or bool(gone)
        if not self._dirty:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        self._ignore_self()
        # Written beside the target and moved into place, so an interrupted
        # command leaves the old cache rather than half a new one.
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps({"version": self.version, "files": self._entries}),
                             encoding="utf-8")
        temporary.replace(self.path)
        self._dirty = False

    def _ignore_self(self) -> None:
        """`.fence` is staged wholesale, and derived data doesn't belong in a commit."""
        ignore = self.dir / ".gitignore"
        if not ignore.exists():
            ignore.write_text("cache.json\ncache.json.tmp\n", encoding="utf-8", newline="\n")


def _sha(source: str) -> str:
    return hashlib.sha1(source.encode("utf-8", "replace")).hexdigest()[:16]

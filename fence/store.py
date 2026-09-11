"""Notes live as small JSON files under .fence/, reviewed and versioned with the code."""

import hashlib
import json
import time
from pathlib import Path

from . import FenceError


class Store:
    def __init__(self, root: Path):
        self.dir = root / ".fence"
        self.notes_dir = self.dir / "notes"
        self.retired_dir = self.dir / "retired"

    def notes(self) -> list[dict]:
        if not self.notes_dir.is_dir():
            return []
        notes = [json.loads(p.read_text(encoding="utf-8")) for p in self.notes_dir.glob("*.json")]
        return sorted(notes, key=lambda n: (n["anchor"]["path"], n["anchor"]["line"]))

    def get(self, prefix: str) -> dict:
        hits = [n for n in self.notes() if n["id"].startswith(prefix)]
        if not hits:
            raise FenceError(f"no note matching {prefix!r}")
        if len(hits) > 1:
            raise FenceError(f"{prefix!r} matches {len(hits)} notes; use more of the id")
        return hits[0]

    def create(self, reason: str, source: str | None, author: str | None,
               snippet: str, anchor: dict) -> dict:
        seed = f"{anchor['path']}:{anchor['line']}:{reason}:{time.time_ns()}"
        note = {
            "id": hashlib.sha1(seed.encode()).hexdigest()[:8],
            "reason": reason,
            "source": source,
            "author": author,
            "created": time.strftime("%Y-%m-%d"),
            "snippet": snippet,
            "anchor": anchor,
        }
        self.save(note)
        return note

    def save(self, note: dict) -> None:
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        _write(self.notes_dir / f"{note['id']}.json", note)

    def retire(self, note: dict, why: str) -> None:
        """Keep retired notes: why a fence came down is worth remembering too."""
        self.retired_dir.mkdir(parents=True, exist_ok=True)
        retired = dict(note, retired=time.strftime("%Y-%m-%d"), retired_because=why)
        _write(self.retired_dir / f"{note['id']}.json", retired)
        (self.notes_dir / f"{note['id']}.json").unlink()


def _write(path: Path, note: dict) -> None:
    # Keep the token list on one line so the file stays readable in a diff.
    anchor = dict(note["anchor"], tokens="__TOKENS__")
    text = json.dumps(dict(note, anchor=anchor), indent=2, ensure_ascii=False)
    text = text.replace('"__TOKENS__"', json.dumps(note["anchor"]["tokens"], ensure_ascii=False))
    path.write_text(text + "\n", encoding="utf-8", newline="\n")

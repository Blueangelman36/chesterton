"""Notes live as small JSON files under .fence/, reviewed and versioned with the code."""

import hashlib
import json
import time
import uuid
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
        notes = [_read(p) for p in self.notes_dir.glob("*.json")]
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


def _read(path: Path) -> dict:
    """A hand-edited note should name the file that is wrong, not raise a traceback."""
    try:
        note = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise FenceError(f"{path.name} is not readable as a note: {e}") from e
    if not isinstance(note, dict) or "id" not in note or "path" not in note.get("anchor", {}):
        raise FenceError(f"{path.name} is missing the fields of a note (id, anchor)")
    return note


def _write(path: Path, note: dict) -> None:
    # Keep the token list on one line so the file stays readable in a diff. The
    # placeholder is random because a fixed one could appear inside the note's
    # own reason or snippet, which are written before it.
    placeholder = f"__TOKENS_{uuid.uuid4().hex}__"
    anchor = dict(note["anchor"], tokens=placeholder)
    text = json.dumps(dict(note, anchor=anchor), indent=2, ensure_ascii=False)
    text = text.replace(json.dumps(placeholder),
                        json.dumps(note["anchor"]["tokens"], ensure_ascii=False))
    path.write_text(text + "\n", encoding="utf-8", newline="\n")

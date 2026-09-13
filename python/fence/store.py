"""Notes live as small JSON files under .fence/, reviewed and versioned with the code.

A note carries one anchor per fingerprint scheme, keyed by version. Each build
reads its own, and writing back replaces only its own — so a repository used by
both implementations keeps both, and neither keeps re-pinning what the other
just wrote. See docs/FORMAT.md.
"""

import hashlib
import json
import time
import uuid
from pathlib import Path

from . import FenceError
from . import anchor as A


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
            "anchors": {str(anchor["version"]): anchor},
            "anchor": anchor,
        }
        self.save(note)
        return note

    def save(self, note: dict) -> None:
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        _write(self.notes_dir / f"{note['id']}.json", _merged(note))

    def retire(self, note: dict, why: str) -> None:
        """Keep retired notes: why a fence came down is worth remembering too."""
        self.retired_dir.mkdir(parents=True, exist_ok=True)
        retired = dict(_merged(note), retired=time.strftime("%Y-%m-%d"), retired_because=why)
        _write(self.retired_dir / f"{note['id']}.json", retired)
        (self.notes_dir / f"{note['id']}.json").unlink()


def _read(path: Path) -> dict:
    """A hand-edited note should name the file that is wrong, not raise a traceback."""
    try:
        note = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise FenceError(f"{path.name} is not readable as a note: {e}") from e
    if not isinstance(note, dict) or "id" not in note:
        raise FenceError(f"{path.name} is missing the fields of a note (id, anchors)")
    _normalise(note)
    if "path" not in note.get("anchor", {}):
        raise FenceError(f"{path.name} has no anchor this build can read")
    return note


def _normalise(note: dict) -> None:
    """On disk a note holds every scheme's anchor; commands work with one.

    Ours if it has one. Otherwise any, because reporting where a foreign note
    probably lives is more use than refusing to talk about it.
    """
    anchors = note.get("anchors")
    if not isinstance(anchors, dict) or not anchors:
        legacy = note.get("anchor")
        anchors = {str(legacy.get("version")): legacy} if isinstance(legacy, dict) else {}
    note["anchors"] = anchors
    ours = anchors.get(str(A.ANCHOR_VERSION))
    note["anchor"] = ours if ours is not None else next(iter(anchors.values()), {})


def _merged(note: dict) -> dict:
    """Write our scheme's anchor back without disturbing anybody else's."""
    anchors = dict(note.get("anchors") or {})
    anchor = note.get("anchor") or {}
    if anchor.get("version") == A.ANCHOR_VERSION:
        anchors[str(A.ANCHOR_VERSION)] = anchor
    merged = {key: value for key, value in note.items() if key != "anchor"}
    merged["anchors"] = anchors
    return merged


def _write(path: Path, note: dict) -> None:
    # Keep each token list on one line so the file stays readable in a diff. The
    # placeholders are random because a fixed one could appear inside the note's
    # own reason or snippet, which are written before the anchors.
    tokens = {}
    anchors = {}
    for version, anchor in note.get("anchors", {}).items():
        placeholder = f"__TOKENS_{uuid.uuid4().hex}__"
        tokens[placeholder] = anchor.get("tokens", [])
        anchors[version] = dict(anchor, tokens=placeholder)
    text = json.dumps(dict(note, anchors=anchors), indent=2, ensure_ascii=False)
    for placeholder, listed in tokens.items():
        text = text.replace(json.dumps(placeholder), json.dumps(listed, ensure_ascii=False))
    path.write_text(text + "\n", encoding="utf-8", newline="\n")


def new_id(path: str, line: int, reason: str) -> str:
    seed = f"{path}:{line}:{reason}:{time.time_ns()}"
    return hashlib.sha1(seed.encode()).hexdigest()[:8]

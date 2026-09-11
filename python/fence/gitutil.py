"""Thin wrappers around the git CLI."""

import re
import subprocess
from pathlib import Path

TRAILER = re.compile(r"^(co-authored-by|signed-off-by):", re.IGNORECASE)


class GitError(RuntimeError):
    pass


def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if check and proc.returncode:
        raise GitError(proc.stderr.strip() or f"git {args[0]} failed")
    return proc.stdout


def repo_root() -> Path:
    return Path(git("rev-parse", "--show-toplevel").strip())


def hooks_dir(root: Path) -> Path:
    """Respects core.hooksPath."""
    path = Path(git("rev-parse", "--git-path", "hooks", cwd=root).strip())
    return path if path.is_absolute() else root / path


def staged_paths(root: Path) -> list[str]:
    out = git("diff", "--cached", "--name-only", "--no-renames", "-z", cwd=root)
    return [p for p in out.split("\0") if p]


def blame(root: Path, path: str, line: int) -> tuple[str, str]:
    """The commit that last wrote a line, and its message minus trailers."""
    out = git("blame", "--porcelain", "-L", f"{line},{line}", "--", path, cwd=root)
    sha = out.split(" ", 1)[0]
    if set(sha) == {"0"}:
        raise GitError(f"{path}:{line} isn't committed yet, so there's no commit message to borrow")
    message = git("log", "-1", "--format=%B", sha, cwd=root)
    kept = [line for line in message.splitlines() if not TRAILER.match(line)]
    return sha, "\n".join(kept).strip()


class WorktreeReader:
    """Files as they are on disk."""

    def __init__(self, root: Path):
        self.root = root

    def read(self, path: str) -> str | None:
        file = self.root / path
        return file.read_text(encoding="utf-8", errors="replace") if file.is_file() else None

    def paths(self) -> list[str]:
        out = git("ls-files", "--cached", "--others", "--exclude-standard", "-z", cwd=self.root)
        return [p for p in out.split("\0") if p]


class IndexReader:
    """Files as they are staged: what the commit will actually contain."""

    def __init__(self, root: Path):
        self.root = root

    def read(self, path: str) -> str | None:
        proc = subprocess.run(["git", "show", f":{path}"], cwd=self.root, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
        return proc.stdout if proc.returncode == 0 else None

    def paths(self) -> list[str]:
        out = git("ls-files", "--cached", "-z", cwd=self.root)
        return [p for p in out.split("\0") if p]

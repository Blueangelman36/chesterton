//! Thin wrappers around the git CLI.

use std::path::{Path, PathBuf};
use std::process::Command;

use crate::anchor::Source;

/// A problem worth showing the user as-is, without a backtrace.
#[derive(Debug)]
pub struct Error(pub String);

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        Error(e.to_string())
    }
}

pub fn git(args: &[&str], cwd: &Path) -> Result<String, Error> {
    let output = Command::new("git")
        .args(args)
        .current_dir(cwd)
        .output()
        .map_err(|e| Error(format!("could not run git: {e}")))?;
    if !output.status.success() {
        let complaint = String::from_utf8_lossy(&output.stderr).trim().to_string();
        return Err(Error(if complaint.is_empty() {
            format!("git {} failed", args.first().copied().unwrap_or("command"))
        } else {
            complaint
        }));
    }
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

pub fn repo_root() -> Result<PathBuf, Error> {
    let here = std::env::current_dir()?;
    Ok(PathBuf::from(
        git(&["rev-parse", "--show-toplevel"], &here)?.trim(),
    ))
}

/// Respects core.hooksPath.
pub fn hooks_dir(root: &Path) -> Result<PathBuf, Error> {
    let named = PathBuf::from(git(&["rev-parse", "--git-path", "hooks"], root)?.trim());
    Ok(if named.is_absolute() { named } else { root.join(named) })
}

pub fn staged_paths(root: &Path) -> Result<Vec<String>, Error> {
    let out = git(
        &["diff", "--cached", "--name-only", "--no-renames", "-z"],
        root,
    )?;
    Ok(split_nul(&out))
}

pub fn config(root: &Path, key: &str) -> Option<String> {
    let value = git(&["config", key], root).ok()?.trim().to_string();
    if value.is_empty() { None } else { Some(value) }
}

pub fn stage_notes(root: &Path) -> Result<(), Error> {
    git(&["add", "-A", "--", ".fence"], root).map(|_| ())
}

/// The commit that last wrote a line, and its message minus trailers.
pub fn blame(root: &Path, path: &str, line: usize) -> Result<(String, String), Error> {
    let span = format!("{line},{line}");
    let out = git(&["blame", "--porcelain", "-L", &span, "--", path], root)?;
    let sha = out.split_whitespace().next().unwrap_or("").to_string();
    if sha.is_empty() || sha.chars().all(|c| c == '0') {
        return Err(Error(format!(
            "{path}:{line} isn't committed yet, so there's no commit message to borrow"
        )));
    }
    let message = git(&["log", "-1", "--format=%B", &sha], root)?;
    let kept: Vec<&str> = message
        .lines()
        .filter(|line| {
            let lower = line.to_ascii_lowercase();
            !lower.starts_with("co-authored-by:") && !lower.starts_with("signed-off-by:")
        })
        .collect();
    Ok((sha, kept.join("\n").trim().to_string()))
}

fn split_nul(text: &str) -> Vec<String> {
    text.split('\0')
        .filter(|piece| !piece.is_empty())
        .map(str::to_string)
        .collect()
}

/// Files as they are on disk.
pub struct Worktree {
    pub root: PathBuf,
}

impl Source for Worktree {
    fn read(&self, path: &str) -> Option<String> {
        std::fs::read_to_string(self.root.join(path)).ok()
    }

    fn paths(&self) -> Vec<String> {
        git(
            &["ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            &self.root,
        )
        .map(|out| split_nul(&out))
        .unwrap_or_default()
    }
}

/// Files as they are staged: what the commit will actually contain.
pub struct Staged {
    pub root: PathBuf,
}

impl Source for Staged {
    fn read(&self, path: &str) -> Option<String> {
        let output = Command::new("git")
            .args(["show", &format!(":{path}")])
            .current_dir(&self.root)
            .output()
            .ok()?;
        if output.status.success() {
            Some(String::from_utf8_lossy(&output.stdout).into_owned())
        } else {
            None
        }
    }

    fn paths(&self) -> Vec<String> {
        git(&["ls-files", "--cached", "-z"], &self.root)
            .map(|out| split_nul(&out))
            .unwrap_or_default()
    }
}

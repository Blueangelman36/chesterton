//! A parse cache, so recording a note doesn't have to read the whole repository.
//!
//! Knowing how many copies of a statement live elsewhere means looking at every
//! file. Parsing them takes seconds on a large project; reading their bytes and
//! hashing them takes a fraction of one. So a file's fingerprint counts are kept
//! against a hash of its contents, and only files that actually changed are
//! parsed again.
//!
//! Derived data: delete it and the next command builds it back. The format
//! matches the Python implementation's, though neither reads the other's file --
//! the fingerprints inside are not comparable anyway.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::anchor::{self, Lang, Source};

#[derive(Clone, Default, Serialize, Deserialize)]
pub struct Entry {
    pub sha: String,
    pub exact: BTreeMap<String, usize>,
    pub shape: BTreeMap<String, usize>,
}

#[derive(Default, Serialize, Deserialize)]
struct Stored {
    version: u32,
    files: BTreeMap<String, Entry>,
}

pub struct Cache {
    dir: PathBuf,
    path: PathBuf,
    version: u32,
    writable: bool,
    stored: Stored,
    dirty: bool,
}

impl Cache {
    /// Read-only is for the hook: a commit should not be writing files.
    pub fn open(root: &Path, version: u32, writable: bool) -> Cache {
        let dir = root.join(".fence");
        let path = dir.join("cache.json");
        let stored = std::fs::read_to_string(&path)
            .ok()
            .and_then(|text| serde_json::from_str::<Stored>(&text).ok())
            // Fingerprints from another scheme are not comparable with these, so
            // a version change throws the whole thing away.
            .filter(|stored| stored.version == version)
            .unwrap_or_default();
        Cache { dir, path, version, writable, stored, dirty: false }
    }

    fn get(&self, path: &str, source: &str) -> Option<&Entry> {
        self.stored
            .files
            .get(path)
            .filter(|entry| entry.sha == anchor::hash(source))
    }

    fn put(&mut self, path: &str, entry: Entry) {
        if self.writable {
            self.stored.files.insert(path.to_string(), entry);
            self.dirty = true;
        }
    }

    fn save(&mut self, keep: &BTreeSet<String>) {
        if !self.writable {
            return;
        }
        let gone: Vec<String> = self
            .stored
            .files
            .keys()
            .filter(|path| !keep.contains(*path))
            .cloned()
            .collect();
        if !gone.is_empty() {
            for path in gone {
                self.stored.files.remove(&path);
            }
            self.dirty = true;
        }
        if !self.dirty {
            return;
        }
        self.stored.version = self.version;
        if std::fs::create_dir_all(&self.dir).is_err() {
            return;
        }
        // `.fence` is staged wholesale, and derived data does not belong in a commit.
        let ignore = self.dir.join(".gitignore");
        if !ignore.exists() {
            let _ = std::fs::write(&ignore, "cache.json\ncache.json.tmp\n");
        }
        if let Ok(text) = serde_json::to_string(&self.stored) {
            // Written beside the target and moved into place, so an interrupted
            // command leaves the old cache rather than half a new one.
            let temporary = self.path.with_extension("json.tmp");
            if std::fs::write(&temporary, text).is_ok() {
                let _ = std::fs::rename(&temporary, &self.path);
            }
        }
        self.dirty = false;
    }
}

/// How many times each fingerprint appears in the repository, and where.
pub struct Tally {
    per_file: HashMap<String, Entry>,
    exact: HashMap<String, usize>,
    shape: HashMap<String, usize>,
}

impl Tally {
    pub fn build(source: &dyn Source, cache: &mut Cache) -> Tally {
        let mut per_file: HashMap<String, Entry> = HashMap::new();
        let mut seen = BTreeSet::new();

        for path in source.paths() {
            if Lang::of(&path).is_none() {
                continue;
            }
            let text = match source.read(&path) {
                Some(text) => text,
                None => continue,
            };
            if !anchor::worth_reading(&path, &text) {
                continue;
            }
            seen.insert(path.clone());
            if let Some(entry) = cache.get(&path, &text) {
                per_file.insert(path.clone(), entry.clone());
                continue;
            }
            let mut entry = Entry { sha: anchor::hash(&text), ..Default::default() };
            if let Some(found) = anchor::candidates(&path, &text) {
                for candidate in &found {
                    *entry.exact.entry(candidate.exact.clone()).or_insert(0) += 1;
                    *entry.shape.entry(candidate.shape.clone()).or_insert(0) += 1;
                }
            }
            cache.put(&path, entry.clone());
            per_file.insert(path, entry);
        }
        cache.save(&seen);

        let mut exact: HashMap<String, usize> = HashMap::new();
        let mut shape: HashMap<String, usize> = HashMap::new();
        for entry in per_file.values() {
            for (fingerprint, count) in &entry.exact {
                *exact.entry(fingerprint.clone()).or_insert(0) += count;
            }
            for (fingerprint, count) in &entry.shape {
                *shape.entry(fingerprint.clone()).or_insert(0) += count;
            }
        }
        Tally { per_file, exact, shape }
    }

    /// How many copies of these fingerprints live in files other than this one.
    pub fn copies_elsewhere(&self, path: &str, exact: &str, shape: &str) -> (usize, usize) {
        let mine = self.per_file.get(path);
        let here_exact = mine.and_then(|e| e.exact.get(exact)).copied().unwrap_or(0);
        let here_shape = mine.and_then(|e| e.shape.get(shape)).copied().unwrap_or(0);
        (
            self.exact.get(exact).copied().unwrap_or(0).saturating_sub(here_exact),
            self.shape.get(shape).copied().unwrap_or(0).saturating_sub(here_shape),
        )
    }
}

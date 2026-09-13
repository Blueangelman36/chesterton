//! Notes live as small JSON files under .fence/, reviewed and versioned with the code.
//!
//! The format is specified in docs/FORMAT.md and shared with the Python
//! implementation: a note written by either must be readable by the other.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

use crate::anchor::{self, Anchor};
use crate::git::Error;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Note {
    pub id: String,
    pub reason: String,
    pub source: Option<String>,
    pub author: Option<String>,
    pub created: String,
    pub snippet: String,
    /// One anchor per fingerprint scheme, keyed by version. This is what is stored.
    #[serde(default)]
    pub anchors: BTreeMap<String, Anchor>,
    /// The one this build reads: ours if the note has it, otherwise whichever
    /// there is, so a foreign note can still be talked about. Derived on load
    /// and merged back on save, never stored on its own.
    #[serde(skip)]
    pub anchor: Anchor,
    /// Notes written before a note could carry more than one scheme.
    #[serde(default, rename = "anchor", skip_serializing)]
    pub(crate) legacy: Option<Anchor>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub retired: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub retired_because: Option<String>,
}

impl Note {
    fn normalise(&mut self) {
        if self.anchors.is_empty() {
            if let Some(anchor) = self.legacy.take() {
                self.anchors.insert(anchor.version.to_string(), anchor);
            }
        }
        let ours = anchor::ANCHOR_VERSION.to_string();
        self.anchor = self
            .anchors
            .get(&ours)
            .or_else(|| self.anchors.values().next())
            .cloned()
            .unwrap_or_default();
    }

    /// Our scheme's anchor written back without disturbing anybody else's.
    fn merged(&self) -> Note {
        let mut copy = self.clone();
        if self.anchor.version == anchor::ANCHOR_VERSION {
            copy.anchors
                .insert(anchor::ANCHOR_VERSION.to_string(), self.anchor.clone());
        }
        copy.legacy = None;
        copy
    }
}

pub struct Store {
    pub dir: PathBuf,
    pub notes_dir: PathBuf,
    pub retired_dir: PathBuf,
}

impl Store {
    pub fn new(root: &Path) -> Self {
        let dir = root.join(".fence");
        Store {
            notes_dir: dir.join("notes"),
            retired_dir: dir.join("retired"),
            dir,
        }
    }

    pub fn notes(&self) -> Result<Vec<Note>, Error> {
        let mut notes = Vec::new();
        let entries = match std::fs::read_dir(&self.notes_dir) {
            Ok(entries) => entries,
            Err(_) => return Ok(notes),
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.extension().and_then(|e| e.to_str()) != Some("json") {
                continue;
            }
            let text = std::fs::read_to_string(&path)?;
            let name = path.file_name().unwrap_or_default().to_string_lossy().to_string();
            // A hand-edited note should name the file that is wrong, not panic.
            let mut note: Note = serde_json::from_str(&text)
                .map_err(|e| Error(format!("{name} is not readable as a note: {e}")))?;
            note.normalise();
            if note.anchor.path.is_empty() {
                return Err(Error(format!("{name} has no anchor this build can read")));
            }
            notes.push(note);
        }
        notes.sort_by(|a, b| {
            (&a.anchor.path, a.anchor.line).cmp(&(&b.anchor.path, b.anchor.line))
        });
        Ok(notes)
    }

    pub fn get(&self, prefix: &str) -> Result<Note, Error> {
        let hits: Vec<Note> = self
            .notes()?
            .into_iter()
            .filter(|note| note.id.starts_with(prefix))
            .collect();
        match hits.len() {
            0 => Err(Error(format!("no note matching {prefix:?}"))),
            1 => Ok(hits.into_iter().next().expect("just checked there is one")),
            many => Err(Error(format!(
                "{prefix:?} matches {many} notes; use more of the id"
            ))),
        }
    }

    pub fn save(&self, note: &Note) -> Result<(), Error> {
        std::fs::create_dir_all(&self.notes_dir)?;
        write_note(&self.notes_dir.join(format!("{}.json", note.id)), &note.merged())
    }

    /// Keep retired notes: why a fence came down is worth remembering too.
    pub fn retire(&self, note: &Note, why: &str) -> Result<(), Error> {
        std::fs::create_dir_all(&self.retired_dir)?;
        let mut retired = note.merged();
        retired.retired = Some(today());
        retired.retired_because = Some(why.to_string());
        write_note(&self.retired_dir.join(format!("{}.json", note.id)), &retired)?;
        std::fs::remove_file(self.notes_dir.join(format!("{}.json", note.id)))?;
        Ok(())
    }
}

fn write_note(path: &Path, note: &Note) -> Result<(), Error> {
    let text = serde_json::to_string_pretty(note)
        .map_err(|e| Error(format!("could not write {}: {e}", path.display())))?;
    std::fs::write(path, text + "\n")?;
    Ok(())
}

pub fn new_id(path: &str, line: usize, reason: &str) -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|since| since.as_nanos())
        .unwrap_or(0);
    anchor::hash(&format!("{path}:{line}:{reason}:{nanos}"))[..8].to_string()
}

pub fn today() -> String {
    let days = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|since| (since.as_secs() / 86_400) as i64)
        .unwrap_or(0);
    let (year, month, day) = civil_from_days(days);
    format!("{year:04}-{month:02}-{day:02}")
}

/// Days since the epoch to a calendar date, so a date costs no dependency.
fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let shifted = days + 719_468;
    let era = if shifted >= 0 { shifted } else { shifted - 146_096 } / 146_097;
    let day_of_era = shifted - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let shifted_month = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * shifted_month + 2) / 5 + 1;
    let month = if shifted_month < 10 { shifted_month + 3 } else { shifted_month - 9 };
    (if month <= 2 { year + 1 } else { year }, month, day)
}

#[cfg(test)]
mod tests {
    use super::civil_from_days;

    #[test]
    fn dates_come_out_right() {
        assert_eq!(civil_from_days(0), (1970, 1, 1));
        assert_eq!(civil_from_days(19_723), (2024, 1, 1));
        assert_eq!(civil_from_days(20_708), (2026, 9, 12));
    }
}

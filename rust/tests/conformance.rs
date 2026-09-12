//! The shared conformance cases, run against this implementation.
//!
//! The same files drive the Python suite. If the two disagree, one of them is
//! wrong and the case says which answer was expected.

use fence::anchor::{self, Index};
use serde::Deserialize;
use std::collections::BTreeMap;
use std::path::PathBuf;

#[derive(Debug, Deserialize)]
struct Case {
    name: String,
    expect: String,
    anchor_match: String,
    /// Which language the case is written in; implementations run what they read.
    language: Option<String>,
    expect_file: Option<String>,
    expect_scope: Option<String>,
    before: BTreeMap<String, String>,
    after: BTreeMap<String, String>,
}

#[derive(Debug, Deserialize)]
struct Cases {
    #[serde(default)]
    case: Vec<Case>,
}

fn cases_dir() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..").join("conformance").join("cases")
}

fn load() -> Vec<(String, Case)> {
    let mut out = Vec::new();
    let mut files: Vec<_> = std::fs::read_dir(cases_dir())
        .expect("conformance/cases is missing")
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| path.extension().is_some_and(|e| e == "toml"))
        .collect();
    files.sort();
    for path in files {
        let text = std::fs::read_to_string(&path).expect("unreadable case file");
        let parsed: Cases = toml::from_str(&text).expect("malformed case file");
        let group = path.file_stem().unwrap().to_string_lossy().to_string();
        for case in parsed.case {
            out.push((group.clone(), case));
        }
    }
    out
}

/// Pin the first statement in `before` whose first line contains anchor_match.
fn anchor_for(case: &Case) -> anchor::Anchor {
    let index = Index::from_sources(&case.before);
    for path in case.before.keys() {
        for candidate in index.get(path) {
            let first = candidate.snippet.lines().next().unwrap_or("");
            if first.contains(&case.anchor_match) {
                return anchor::make_anchor(candidate, index.get(path), &index.others(path));
            }
        }
    }
    panic!("no statement matching {:?} in {:?}", case.anchor_match, case.name);
}

#[test]
fn every_case() {
    let cases = load();
    assert!(!cases.is_empty(), "no conformance cases found in {:?}", cases_dir());

    // Printed so a passing run still says what it covered: a case that silently
    // stopped running looks exactly like a case that passes.
    let mut per_language: BTreeMap<String, usize> = BTreeMap::new();
    for (_, case) in &cases {
        let language = case.language.clone().unwrap_or_else(|| "python".to_string());
        *per_language.entry(language).or_insert(0) += 1;
    }
    let covered: Vec<String> = per_language
        .iter()
        .map(|(language, count)| format!("{language}={count}"))
        .collect();
    println!("conformance: {} cases ({})", cases.len(), covered.join(", "));

    let mut failures = Vec::new();
    for (group, case) in &cases {
        let found = anchor::locate(&anchor_for(case), &Index::from_sources(&case.after));
        let mut problems = Vec::new();
        if found.how.as_str() != case.expect {
            problems.push(format!("expected {}, got {}", case.expect, found.how.as_str()));
        }
        if let Some(wanted) = &case.expect_file {
            let actual = found.candidate.as_ref().map(|c| c.path.clone());
            if actual.as_deref() != Some(wanted.as_str()) {
                problems.push(format!("expected file {wanted}, got {actual:?}"));
            }
        }
        if let Some(wanted) = &case.expect_scope {
            let actual = found.candidate.as_ref().map(|c| c.scope.clone());
            if actual.as_deref() != Some(wanted.as_str()) {
                problems.push(format!("expected scope {wanted}, got {actual:?}"));
            }
        }
        if !problems.is_empty() {
            failures.push(format!("{group}: {}\n    {}", case.name, problems.join("\n    ")));
        }
    }

    assert!(
        failures.is_empty(),
        "{} of {} conformance cases failed:\n{}",
        failures.len(),
        cases.len(),
        failures.join("\n")
    );
}

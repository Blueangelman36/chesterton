//! Structural anchors over a tree-sitter parse.
//!
//! A port of the reference implementation in `../python`, holding to the same
//! contract: the cascade, the three lookalike rules, and the verdicts named in
//! `docs/FORMAT.md`. The fingerprints themselves are deliberately not the same
//! bytes as the Python ones -- a different parser hashes differently, which is
//! what the anchor's `version` field exists to record.

use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, HashMap, HashSet};
use tree_sitter::{Node, Parser, Tree};

/// This implementation's fingerprint scheme. Anchors written by the Python one
/// carry version 1, and their hashes are not comparable with these.
pub const ANCHOR_VERSION: u32 = 2;
pub const MIN_DISTINCTIVE_TOKENS: usize = 12;
pub const CHANGED_THRESHOLD: f64 = 0.5;
const SNIPPET_LINES: usize = 12;
const LONG_TOKEN: usize = 32;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum How {
    Ok,
    Renamed,
    Moved,
    Changed,
    Ambiguous,
    Removed,
    Unparseable,
}

impl How {
    pub fn as_str(&self) -> &'static str {
        match self {
            How::Ok => "ok",
            How::Renamed => "renamed",
            How::Moved => "moved",
            How::Changed => "changed",
            How::Ambiguous => "ambiguous",
            How::Removed => "removed",
            How::Unparseable => "unparseable",
        }
    }
}

#[derive(Debug, Clone)]
pub struct Candidate {
    pub path: String,
    pub scope: String,
    pub kind: String,
    pub line: usize,
    pub end_line: usize,
    pub exact: String,
    pub shape: String,
    pub tokens: Vec<String>,
    pub snippet: String,
}

impl Candidate {
    pub fn features(&self) -> HashSet<String> {
        features(&self.tokens)
    }

    fn fingerprint(&self, key: Key) -> &str {
        match key {
            Key::Exact => &self.exact,
            Key::Shape => &self.shape,
        }
    }
}

#[derive(Clone, Copy, PartialEq)]
enum Key {
    Exact,
    Shape,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct Dupes {
    pub exact: usize,
    pub shape: usize,
    pub other_exact: usize,
    pub other_shape: usize,
    pub far_exact: usize,
    pub far_shape: usize,
}

impl Dupes {
    fn same_scope(&self, key: Key) -> usize {
        match key {
            Key::Exact => self.exact,
            Key::Shape => self.shape,
        }
    }

    fn other_scope(&self, key: Key) -> usize {
        match key {
            Key::Exact => self.other_exact,
            Key::Shape => self.other_shape,
        }
    }

    fn far(&self, key: Key) -> usize {
        match key {
            Key::Exact => self.far_exact,
            Key::Shape => self.far_shape,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Anchor {
    pub version: u32,
    pub path: String,
    pub scope: String,
    pub kind: String,
    pub line: usize,
    pub exact: String,
    pub shape: String,
    pub dupes: Dupes,
    pub rival: f64,
    pub tokens: Vec<String>,
}

impl Anchor {
    fn fingerprint(&self, key: Key) -> &str {
        match key {
            Key::Exact => &self.exact,
            Key::Shape => &self.shape,
        }
    }
}

#[derive(Debug, Clone)]
pub struct Match {
    pub how: How,
    pub candidate: Option<Candidate>,
    pub similarity: f64,
}

impl Match {
    fn found(how: How, candidate: &Candidate) -> Self {
        Match { how, candidate: Some(candidate.clone()), similarity: 1.0 }
    }

    fn none(how: How) -> Self {
        Match { how, candidate: None, similarity: 1.0 }
    }
}

/// Every file the implementation can see, parsed.
pub struct Index {
    files: BTreeMap<String, Vec<Candidate>>,
    pub broken: HashSet<String>,
}

impl Index {
    pub fn from_sources(sources: &BTreeMap<String, String>) -> Self {
        let mut files = BTreeMap::new();
        let mut broken = HashSet::new();
        for (path, source) in sources {
            match candidates(path, source) {
                Some(found) => {
                    files.insert(path.clone(), found);
                }
                None => {
                    broken.insert(path.clone());
                    files.insert(path.clone(), Vec::new());
                }
            }
        }
        Index { files, broken }
    }

    pub fn get(&self, path: &str) -> &[Candidate] {
        self.files.get(path).map(Vec::as_slice).unwrap_or(&[])
    }

    pub fn others(&self, path: &str) -> Vec<&Candidate> {
        self.files
            .iter()
            .filter(|(p, _)| p.as_str() != path)
            .flat_map(|(_, found)| found.iter())
            .collect()
    }
}

pub fn parse(source: &str) -> Option<Tree> {
    let mut parser = Parser::new();
    parser.set_language(&tree_sitter_python::LANGUAGE.into()).ok()?;
    parser.parse(source, None)
}

/// Every statement in a file, outermost first, with its fingerprints.
/// `None` when the file does not parse.
pub fn candidates(path: &str, source: &str) -> Option<Vec<Candidate>> {
    let tree = parse(source)?;
    let root = tree.root_node();
    if root.has_error() {
        return None;
    }
    let lines: Vec<&str> = source.split('\n').collect();
    let mut out = Vec::new();
    walk(root, path, source, &lines, "", &mut out);
    Some(out)
}

/// Statements are the named children of a block or module, which is exactly
/// what a statement is in the grammar, without naming every statement kind.
fn walk(node: Node, path: &str, source: &str, lines: &[&str], scope: &str, out: &mut Vec<Candidate>) {
    let container = matches!(node.kind(), "block" | "module");
    let mut cursor = node.walk();
    for child in node.named_children(&mut cursor) {
        if child.kind() == "comment" {
            continue;
        }
        if container {
            out.push(candidate(child, path, source, lines, scope));
        }
        let inner = match child.kind() {
            "function_definition" | "class_definition" => match name_of(child, source) {
                Some(name) if scope.is_empty() => name,
                Some(name) => format!("{scope}.{name}"),
                None => scope.to_string(),
            },
            _ => scope.to_string(),
        };
        walk(child, path, source, lines, &inner, out);
    }
}

fn name_of(node: Node, source: &str) -> Option<String> {
    node.child_by_field_name("name")?
        .utf8_text(source.as_bytes())
        .ok()
        .map(str::to_string)
}

fn candidate(node: Node, path: &str, source: &str, lines: &[&str], scope: &str) -> Candidate {
    let line = node.start_position().row + 1;
    let end_line = node.end_position().row + 1;
    let mut tokens = Vec::new();
    collect_tokens(node, source, &mut tokens);

    let mut exact = String::new();
    serialize(node, source, &mut Naming::Exact, &mut exact);
    let mut shape = String::new();
    serialize(node, source, &mut Naming::Shape(HashMap::new()), &mut shape);

    Candidate {
        path: path.to_string(),
        scope: scope.to_string(),
        kind: kind_of(node),
        line,
        end_line,
        exact: hash(&exact),
        shape: hash(&shape),
        tokens,
        snippet: snippet(lines, line, end_line),
    }
}

fn kind_of(node: Node) -> String {
    if node.kind() == "expression_statement" {
        if let Some(first) = node.named_child(0) {
            return format!("expression_statement.{}", first.kind());
        }
    }
    node.kind().to_string()
}

enum Naming {
    Exact,
    Shape(HashMap<String, String>),
}

impl Naming {
    fn identifier(&mut self, text: &str) -> String {
        match self {
            Naming::Exact => text.to_string(),
            Naming::Shape(seen) => {
                let next = seen.len();
                seen.entry(text.to_string()).or_insert_with(|| format!("v{next}")).clone()
            }
        }
    }
}

/// A canonical rendering of the subtree. Whitespace never appears in the tree,
/// so formatting is ignored for free; comments have to be dropped by hand.
fn serialize(node: Node, source: &str, naming: &mut Naming, out: &mut String) {
    if node.kind() == "comment" {
        return;
    }
    if node.child_count() == 0 || node.kind() == "string" {
        let text = node.utf8_text(source.as_bytes()).unwrap_or("");
        if node.kind() == "identifier" {
            out.push_str(&naming.identifier(text));
        } else {
            out.push_str(node.kind());
            out.push(':');
            out.push_str(text);
        }
        out.push(' ');
        return;
    }
    out.push_str(node.kind());
    out.push('(');
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        serialize(child, source, naming, out);
    }
    out.push(')');
}

fn collect_tokens(node: Node, source: &str, out: &mut Vec<String>) {
    if node.kind() == "comment" {
        return;
    }
    if node.child_count() == 0 || node.kind() == "string" {
        let text = node.utf8_text(source.as_bytes()).unwrap_or("");
        if text.is_empty() {
            return;
        }
        out.push(if text.chars().count() > LONG_TOKEN {
            format!("<{}>", hash(text))
        } else {
            text.to_string()
        });
        return;
    }
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        collect_tokens(child, source, out);
    }
}

fn snippet(lines: &[&str], line: usize, end_line: usize) -> String {
    let body: Vec<&str> = lines[line - 1..end_line.min(lines.len())].to_vec();
    let shown: Vec<&str> = body.iter().take(SNIPPET_LINES).copied().collect();
    let indent = shown
        .iter()
        .filter(|l| !l.trim().is_empty())
        .map(|l| l.len() - l.trim_start().len())
        .min()
        .unwrap_or(0);
    let mut text = shown
        .iter()
        .map(|l| if l.len() >= indent { &l[indent..] } else { l.trim_start() })
        .map(|l| l.trim_end_matches('\r'))
        .collect::<Vec<_>>()
        .join("\n");
    if body.len() > SNIPPET_LINES {
        text.push_str("\n...");
    }
    text
}

/// Tokens plus adjacent pairs: order-aware, but tolerant of small edits.
pub fn features(tokens: &[String]) -> HashSet<String> {
    let mut grams: HashSet<String> = tokens.iter().cloned().collect();
    for pair in tokens.windows(2) {
        grams.insert(format!("{}\u{1}{}", pair[0], pair[1]));
    }
    grams
}

pub fn similarity(a: &HashSet<String>, b: &HashSet<String>) -> f64 {
    let union = a.union(b).count();
    if union == 0 {
        return 1.0;
    }
    a.intersection(b).count() as f64 / union as f64
}

fn distinctive(tokens: &[String]) -> bool {
    tokens.len() >= MIN_DISTINCTIVE_TOKENS
}

fn same_statement(a: &Candidate, b: &Candidate) -> bool {
    a.path == b.path && a.line == b.line && a.exact == b.exact
}

fn near<'a>(cands: impl IntoIterator<Item = &'a Candidate>, scope: &str, distinctive: bool) -> Vec<&'a Candidate> {
    cands.into_iter().filter(|c| distinctive || c.scope == scope).collect()
}

/// Everything needed to find `target` again, including how crowded its
/// neighborhood is. `elsewhere` is the statements of every other file: counting
/// the copies already there is what later tells a real move from boilerplate.
pub fn make_anchor(target: &Candidate, siblings: &[Candidate], elsewhere: &[&Candidate]) -> Anchor {
    let copies = |pool: &[&Candidate], key: Key| -> usize {
        pool.iter().filter(|c| c.fingerprint(key) == target.fingerprint(key)).count()
    };
    let same_scope: Vec<&Candidate> = siblings.iter().filter(|c| c.scope == target.scope).collect();
    let other_scope: Vec<&Candidate> = siblings.iter().filter(|c| c.scope != target.scope).collect();

    let target_features = target.features();
    let rival = near(siblings, &target.scope, distinctive(&target.tokens))
        .into_iter()
        .filter(|c| !same_statement(c, target) && c.kind == target.kind)
        .map(|c| similarity(&target_features, &c.features()))
        .fold(0.0_f64, f64::max);

    Anchor {
        version: ANCHOR_VERSION,
        path: target.path.clone(),
        scope: target.scope.clone(),
        kind: target.kind.clone(),
        line: target.line,
        exact: target.exact.clone(),
        shape: target.shape.clone(),
        dupes: Dupes {
            exact: copies(&same_scope, Key::Exact),
            shape: copies(&same_scope, Key::Shape),
            other_exact: copies(&other_scope, Key::Exact),
            other_shape: copies(&other_scope, Key::Shape),
            far_exact: copies(elsewhere, Key::Exact),
            far_shape: copies(elsewhere, Key::Shape),
        },
        rival,
        tokens: target.tokens.clone(),
    }
}

/// Find the statement an anchor was made from, in the code as it is now.
pub fn locate(anchor: &Anchor, index: &Index) -> Match {
    if index.broken.contains(&anchor.path) {
        return Match::none(How::Unparseable);
    }
    let home = index.get(&anchor.path);
    let is_distinctive = distinctive(&anchor.tokens);
    let nearby = near(home, &anchor.scope, is_distinctive);

    let closest = |cands: &[&Candidate]| -> Candidate {
        cands
            .iter()
            .min_by_key(|c| {
                (
                    (c.path != anchor.path) as u8,
                    (c.scope != anchor.scope) as u8,
                    c.line.abs_diff(anchor.line),
                )
            })
            .expect("closest called with no candidates")
            .clone()
    };

    if anchor.version == ANCHOR_VERSION {
        for (key, how) in [(Key::Exact, How::Ok), (Key::Shape, How::Renamed)] {
            let hits: Vec<&Candidate> = nearby
                .iter()
                .filter(|c| c.fingerprint(key) == anchor.fingerprint(key))
                .copied()
                .collect();
            let here: Vec<&Candidate> = hits.iter().filter(|c| c.scope == anchor.scope).copied().collect();
            if !here.is_empty() && here.len() >= anchor.dupes.same_scope(key).max(1) {
                return Match::found(how, &closest(&here));
            }
            if !here.is_empty() && key == Key::Exact {
                return Match::found(How::Ambiguous, &closest(&here));
            }
            if !hits.is_empty() && here.is_empty() && hits.len() > anchor.dupes.other_scope(key) {
                return Match::found(How::Moved, &closest(&hits));
            }
        }

        // A move means a copy turned up somewhere that didn't have one before.
        // Boilerplate that was always duplicated elsewhere cannot vouch for code
        // that just disappeared.
        if is_distinctive {
            let far = index.others(&anchor.path);
            for key in [Key::Exact, Key::Shape] {
                let hits: Vec<&Candidate> = far
                    .iter()
                    .filter(|c| c.fingerprint(key) == anchor.fingerprint(key))
                    .copied()
                    .collect();
                if hits.len() > anchor.dupes.far(key) {
                    return Match::found(How::Moved, &closest(&hits));
                }
            }
        }
    }

    // Edited in place: the most similar statement of the same kind, as long as
    // it beats every lookalike that was already there.
    let wanted = features(&anchor.tokens);
    let mut best: Option<(f64, &Candidate)> = None;
    for candidate in nearby.iter().filter(|c| c.kind == anchor.kind) {
        let score = similarity(&wanted, &candidate.features());
        let ranked = score + if candidate.scope == anchor.scope { 0.1 } else { 0.0 };
        let better = match best {
            None => true,
            Some((current, held)) => {
                let held_rank = current + if held.scope == anchor.scope { 0.1 } else { 0.0 };
                ranked > held_rank
                    || (ranked == held_rank
                        && candidate.line.abs_diff(anchor.line) < held.line.abs_diff(anchor.line))
            }
        };
        if better {
            best = Some((score, candidate));
        }
    }
    if let Some((score, candidate)) = best
        && score >= CHANGED_THRESHOLD
        && score > anchor.rival
    {
        return Match { how: How::Changed, candidate: Some(candidate.clone()), similarity: score };
    }
    Match::none(How::Removed)
}

/// FNV-1a. Fingerprints only ever get compared with others from this same
/// implementation, so a short non-cryptographic digest is enough.
fn hash(text: &str) -> String {
    let mut value: u64 = 0xcbf29ce484222325;
    for byte in text.as_bytes() {
        value ^= *byte as u64;
        value = value.wrapping_mul(0x100000001b3);
    }
    format!("{value:016x}")
}

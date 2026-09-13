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
pub const ANCHOR_VERSION: u32 = 4;
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
    /// Written under another implementation's scheme, so its fingerprints mean
    /// nothing here. Not the same as the code having changed, and not grounds
    /// for blocking a commit.
    Foreign,
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
            How::Foreign => "foreign",
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

/// What a statement, a scope and a name are, per language.
///
/// Everything language-specific lives here. Adding a language is a grammar plus
/// answers to these questions, not a change to how anchoring works.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Lang {
    Python,
    TypeScript,
    Tsx,
}

impl Lang {
    pub fn of(path: &str) -> Option<Lang> {
        let lower = path.to_ascii_lowercase();
        for suffix in [".py", ".pyi"] {
            if lower.ends_with(suffix) {
                return Some(Lang::Python);
            }
        }
        for suffix in [".ts", ".mts", ".cts"] {
            if lower.ends_with(suffix) {
                return Some(Lang::TypeScript);
            }
        }
        // JSX lives in .js as often as in .jsx, and the TSX grammar reads both.
        for suffix in [".tsx", ".jsx", ".js", ".mjs", ".cjs"] {
            if lower.ends_with(suffix) {
                return Some(Lang::Tsx);
            }
        }
        None
    }

    fn grammar(self) -> tree_sitter::Language {
        match self {
            Lang::Python => tree_sitter_python::LANGUAGE.into(),
            Lang::TypeScript => tree_sitter_typescript::LANGUAGE_TYPESCRIPT.into(),
            Lang::Tsx => tree_sitter_typescript::LANGUAGE_TSX.into(),
        }
    }

    /// Nodes whose named children are statements.
    fn holds_statements(self, kind: &str) -> bool {
        match self {
            Lang::Python => matches!(kind, "block" | "module"),
            _ => matches!(
                kind,
                "program" | "statement_block" | "class_body" | "switch_case" | "switch_default"
            ),
        }
    }

    /// A leaf in spirit: descending into a string would compare its pieces.
    fn is_atom(self, kind: &str) -> bool {
        match self {
            Lang::Python => kind == "string",
            _ => matches!(kind, "string" | "template_string"),
        }
    }

    fn is_identifier(self, kind: &str) -> bool {
        match self {
            Lang::Python => kind == "identifier",
            _ => matches!(
                kind,
                "identifier"
                    | "property_identifier"
                    | "shorthand_property_identifier"
                    | "shorthand_property_identifier_pattern"
                    | "type_identifier"
            ),
        }
    }

    /// A property name is an attribute wherever it turns up, not only after a dot.
    fn identifier_is_attribute(self, kind: &str) -> bool {
        self != Lang::Python && kind == "property_identifier"
    }

    /// The child holding a name in the attribute namespace rather than the value one.
    fn attribute_child(self, node: Node) -> Option<usize> {
        let field = match self {
            Lang::Python if node.kind() == "attribute" => "attribute",
            Lang::Python => return None,
            _ if node.kind() == "member_expression" => "property",
            _ => return None,
        };
        node.child_by_field_name(field).map(|child| child.id())
    }

    /// The name this node introduces a scope under, if it introduces one.
    fn scope_name(self, node: Node, source: &str) -> Option<String> {
        let named = |n: Node| -> Option<String> {
            n.child_by_field_name("name")?
                .utf8_text(source.as_bytes())
                .ok()
                .map(str::to_string)
        };
        match self {
            Lang::Python => match node.kind() {
                "function_definition" | "class_definition" => named(node),
                _ => None,
            },
            _ => match node.kind() {
                "function_declaration"
                | "generator_function_declaration"
                | "class_declaration"
                | "abstract_class_declaration"
                | "interface_declaration"
                | "enum_declaration"
                | "method_definition" => named(node),
                // `const Chart = () => {...}` is how much of a TypeScript codebase is
                // written; without this, every arrow body would share one scope.
                "variable_declarator" => {
                    let value = node.child_by_field_name("value")?;
                    if matches!(value.kind(), "arrow_function" | "function_expression") {
                        named(node)
                    } else {
                        None
                    }
                }
                _ => None,
            },
        }
    }
}

/// Where an implementation gets files from: the worktree, the index, or a test.
pub trait Source {
    fn read(&self, path: &str) -> Option<String>;
    fn paths(&self) -> Vec<String>;
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
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

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
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

    /// Every file a source can see. Parsing is fast enough here that the Python
    /// implementation's content cache has no counterpart yet.
    pub fn scan(source: &dyn Source) -> Self {
        Index::scan_with(source, &[])
    }

    /// Every file worth reading, plus any named path regardless.
    ///
    /// A note can live wherever somebody put it, so a path asked for by name is
    /// always read. Discovery is choosier: see `worth_reading`.
    pub fn scan_with(source: &dyn Source, always: &[String]) -> Self {
        let mut sources = BTreeMap::new();
        for path in source.paths() {
            if Lang::of(&path).is_none() {
                continue;
            }
            if let Some(text) = source.read(&path) {
                if always.contains(&path) || worth_reading(&path, &text) {
                    sources.insert(path, text);
                }
            }
        }
        Index::from_sources(&sources)
    }

    pub fn scan_paths(source: &dyn Source, paths: &[String]) -> Self {
        let mut sources = BTreeMap::new();
        for path in paths {
            if Lang::of(path).is_some() {
                if let Some(text) = source.read(path) {
                    sources.insert(path.clone(), text);
                }
            }
        }
        Index::from_sources(&sources)
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

/// Vendored and generated code is not where reasons live, and a minified bundle
/// is one statement a megabyte wide: parsing it costs seconds and teaches nothing.
///
/// Measured on django, where the admin's vendored jQuery was most of the time
/// spent on `check` and the best thing `suggest` could find.
pub fn worth_reading(path: &str, source: &str) -> bool {
    const GENERATED: &[&str] = &[
        "/vendor/", "/vendored/", "/node_modules/", "/dist/", "/build/", "/third_party/",
        "/site-packages/", "/.venv/", "/migrations/", ".min.js", ".min.css", ".bundle.js",
        "-min.js", ".generated.", "_pb2.py",
    ];
    let lower = format!("/{}", path.to_ascii_lowercase());
    if GENERATED.iter().any(|part| lower.contains(part)) {
        return false;
    }
    !source.lines().any(|line| line.len() > 2000)
}

/// The smallest statement covering the lines; the outermost one on a tie.
pub fn pick(cands: &[Candidate], start: usize, end: usize) -> Option<&Candidate> {
    cands
        .iter()
        .enumerate()
        .filter(|(_, c)| c.line <= start && c.end_line >= end)
        .min_by_key(|(position, c)| (c.end_line - c.line, *position))
        .map(|(_, c)| c)
}

pub fn scope_label(scope: &str) -> &str {
    if scope.is_empty() { "<module>" } else { scope }
}

pub fn parse(lang: Lang, source: &str) -> Option<Tree> {
    let mut parser = Parser::new();
    parser.set_language(&lang.grammar()).ok()?;
    parser.parse(source, None)
}

/// Every statement in a file, outermost first, with its fingerprints. `None`
/// when the file does not parse, or is in a language this build does not read.
pub fn candidates(path: &str, source: &str) -> Option<Vec<Candidate>> {
    let lang = Lang::of(path)?;
    let tree = parse(lang, source)?;
    let root = tree.root_node();
    if root.has_error() {
        return None;
    }
    let lines: Vec<&str> = source.split('\n').collect();
    let mut out = Vec::new();
    walk(lang, root, path, source, &lines, "", &mut out);
    Some(out)
}

/// Statements are the named children of a block, which is what a statement is in
/// the grammar, without having to name every statement kind in every language.
fn walk(
    lang: Lang,
    node: Node,
    path: &str,
    source: &str,
    lines: &[&str],
    scope: &str,
    out: &mut Vec<Candidate>,
) {
    let container = lang.holds_statements(node.kind());
    let mut cursor = node.walk();
    for child in node.named_children(&mut cursor) {
        if child.kind() == "comment" {
            continue;
        }
        if container {
            out.push(candidate(lang, child, path, source, lines, scope));
        }
        let inner = match lang.scope_name(child, source) {
            Some(name) if scope.is_empty() => name,
            Some(name) => format!("{scope}.{name}"),
            None => scope.to_string(),
        };
        walk(lang, child, path, source, lines, &inner, out);
    }
}

fn candidate(
    lang: Lang,
    node: Node,
    path: &str,
    source: &str,
    lines: &[&str],
    scope: &str,
) -> Candidate {
    let line = node.start_position().row + 1;
    let end_line = node.end_position().row + 1;
    let mut tokens = Vec::new();
    collect_tokens(lang, node, source, &mut tokens);

    let mut exact = String::new();
    serialize(lang, node, source, &mut Naming::Exact, &mut exact, false);
    let mut shape = String::new();
    let mut shape_naming = Naming::Shape { values: HashMap::new(), attrs: HashMap::new() };
    serialize(lang, node, source, &mut shape_naming, &mut shape, false);

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
    /// Values and attributes are numbered separately: renaming a variable leaves
    /// attributes spelled as they were, so where a name is also an attribute
    /// (`Error` alongside `Generic.Error`) one shared namespace shifted every
    /// attribute's number and made an ordinary rename look like a deletion.
    Shape { values: HashMap<String, String>, attrs: HashMap<String, String> },
}

impl Naming {
    fn identifier(&mut self, text: &str, attribute: bool) -> String {
        match self {
            Naming::Exact => text.to_string(),
            Naming::Shape { values, attrs } => {
                let (seen, prefix) = if attribute { (attrs, "a") } else { (values, "v") };
                let next = seen.len();
                seen.entry(text.to_string()).or_insert_with(|| format!("{prefix}{next}")).clone()
            }
        }
    }
}

/// A canonical rendering of the subtree. Whitespace never appears in the tree,
/// so formatting is ignored for free; comments have to be dropped by hand.
fn serialize(
    lang: Lang,
    node: Node,
    source: &str,
    naming: &mut Naming,
    out: &mut String,
    attribute: bool,
) {
    if node.kind() == "comment" {
        return;
    }
    if node.child_count() == 0 || lang.is_atom(node.kind()) {
        let text = node.utf8_text(source.as_bytes()).unwrap_or("");
        if lang.is_identifier(node.kind()) {
            let attribute = attribute || lang.identifier_is_attribute(node.kind());
            out.push_str(&naming.identifier(text, attribute));
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
    // The name after the dot is an attribute, not a value of the same name.
    let attr_child = lang.attribute_child(node);
    let mut cursor = node.walk();
    for child in node.children(&mut cursor) {
        let is_attribute = attr_child == Some(child.id());
        serialize(lang, child, source, naming, out, is_attribute);
    }
    out.push(')');
}

fn collect_tokens(lang: Lang, node: Node, source: &str, out: &mut Vec<String>) {
    if node.kind() == "comment" {
        return;
    }
    if node.child_count() == 0 || lang.is_atom(node.kind()) {
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
        collect_tokens(lang, child, source, out);
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
/// Copies of a statement among candidates from other files, counted the slow way.
///
/// For callers holding a plain list; anything with a tally should ask it instead,
/// since that counts the whole repository once and remembers it.
pub fn far_copies(target: &Candidate, elsewhere: &[&Candidate]) -> (usize, usize) {
    (
        elsewhere.iter().filter(|c| c.exact == target.exact).count(),
        elsewhere.iter().filter(|c| c.shape == target.shape).count(),
    )
}

pub fn make_anchor(target: &Candidate, siblings: &[Candidate], far: (usize, usize)) -> Anchor {
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
            far_exact: far.0,
            far_shape: far.1,
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
            .copied()
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

    // An anchor from another implementation's scheme cannot be compared by
    // fingerprint at all. Saying so is the honest answer: `changed` blames the
    // code, and `removed` would block a commit over a difference between tools.
    let unfamiliar = anchor.version != ANCHOR_VERSION;

    if !unfamiliar {
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
    for candidate in nearby.iter().copied().filter(|c| c.kind == anchor.kind) {
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
    if let Some((score, candidate)) = best {
        if unfamiliar && score >= CHANGED_THRESHOLD {
            return Match {
                how: How::Foreign,
                candidate: Some(candidate.clone()),
                similarity: score,
            };
        }
        if !unfamiliar && score >= CHANGED_THRESHOLD && score > anchor.rival {
            return Match {
                how: How::Changed,
                candidate: Some(candidate.clone()),
                similarity: score,
            };
        }
    }
    Match::none(if unfamiliar { How::Foreign } else { How::Removed })
}

/// FNV-1a. Fingerprints only ever get compared with others from this same
/// implementation, so a short non-cryptographic digest is enough.
#[cfg(test)]
mod tests {
    use super::worth_reading;

    #[test]
    fn vendored_and_minified_files_are_not_where_reasons_live() {
        assert!(!worth_reading("static/vendor/jquery/jquery.min.js", "x=1\n"));
        assert!(!worth_reading("node_modules/x/index.js", "x=1\n"));
        assert!(!worth_reading("app/bundle.js", &"var x=1;".repeat(400)));
        assert!(worth_reading("src/client.ts", "export const x = 1;\n"));
    }
}

pub(crate) fn hash(text: &str) -> String {
    let mut value: u64 = 0xcbf29ce484222325;
    for byte in text.as_bytes() {
        value ^= *byte as u64;
        value = value.wrapping_mul(0x100000001b3);
    }
    format!("{value:016x}")
}

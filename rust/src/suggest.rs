//! Which statements have a reason nobody wrote down?
//!
//! A port of the reference implementation's `suggest`, and the same discipline:
//! the ranking counts, it does not judge. The signals are a comment that explains
//! rather than describes, a timing primitive, a swallowed error, a number somebody
//! chose. The discount matters as much -- a statement written the same way in
//! several other files is a convention, not a decision.
//!
//! Word matching is written out rather than pulled in: the lists are literal
//! words, and a dependency for that would cost more than it saves.

use std::collections::HashMap;

use crate::anchor::{self, Candidate, Index, Lang};

const EXPLAINS: &[&str] = &[
    "because", "why", "so that", "otherwise", "must", "needs to", "has to", "workaround",
    "deliberate", "deliberately", "on purpose", "intentional", "intentionally", "do not remove",
    "keep", "note", "caveat", "vendor", "upstream", "third-party", "bug", "issue", "ticket",
    "quirk", "breaks", "broken", "fails", "failing", "regression", "rate limit", "rate-limit",
    "race", "deadlock", "legacy", "historical", "temporary", "until", "revert",
];

const ENVIRONMENT: &[&str] = &[
    "safari", "firefox", "chrome", "chromium", "webkit", "edge", "ios", "android", "windows",
    "macos", "darwin", "linux", "posix", "cygwin", "msvc", "crlf", "timezone", "dst", "proxy",
    "cors", "tls", "ssl",
];

const TIMING: &[&str] = &[
    "sleep", "timeout", "delay", "wait", "retry", "retries", "backoff", "debounce", "throttle",
    "poll", "polling", "settimeout", "setinterval", "deadline", "expires",
];

/// Above this many copies elsewhere, a statement is how this project writes
/// things rather than a decision anybody made.
const CONVENTION: usize = 3;
/// Generous, because a draft that starts mid-sentence is worse than a long one:
/// a doc comment is one thought and cutting it from the bottom ruins it.
const COMMENT_LINES: usize = 12;

#[derive(Debug, Clone)]
pub struct Suggestion {
    pub path: String,
    pub line: usize,
    pub end_line: usize,
    pub kind: String,
    pub scope: String,
    pub text: String,
    pub score: i32,
    pub reasons: Vec<String>,
    pub draft: Option<String>,
}

impl Suggestion {
    pub fn command(&self) -> String {
        let where_ = format!("{}:{}", self.path, self.line);
        match &self.draft {
            // A quote inside the draft would end the argument early.
            Some(draft) => format!("fence add {where_} -m \"{}\"", draft.replace('"', "'")),
            None => format!("fence add {where_} --from-blame"),
        }
    }
}

fn is_word_char(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// Whole-word match, except for phrases, where the spaces do the bounding.
fn mentions(haystack: &str, needles: &[&str]) -> bool {
    needles.iter().any(|needle| {
        if needle.contains(' ') {
            return haystack.contains(needle);
        }
        haystack.match_indices(needle).any(|(start, matched)| {
            let before = haystack[..start].chars().next_back();
            let after = haystack[start + matched.len()..].chars().next();
            !before.is_some_and(is_word_char) && !after.is_some_and(is_word_char)
        })
    })
}

/// Did somebody pick this number, or is it structure?
///
/// 0.05 and 250 and 8420 are decisions. 0, 1, 2 and their decimal spellings are
/// how code is shaped: a default of 1.0 is not a claim about the world.
pub fn chosen_number(text: &str) -> bool {
    let chars: Vec<char> = text.chars().collect();
    let mut at = 0;
    while at < chars.len() {
        if !chars[at].is_ascii_digit() {
            at += 1;
            continue;
        }
        let start = at;
        while at < chars.len()
            && (chars[at].is_ascii_digit()
                || (chars[at] == '.' && chars.get(at + 1).is_some_and(char::is_ascii_digit)))
        {
            at += 1;
        }
        let bounded = |c: Option<&char>| !c.is_some_and(|c| is_word_char(*c) || *c == '.');
        if bounded(start.checked_sub(1).and_then(|i| chars.get(i))) && bounded(chars.get(at)) {
            let token: String = chars[start..at].iter().collect();
            if token.contains('.') {
                if let Ok(value) = token.parse::<f64>() {
                    if value != 0.0 && value != 1.0 && value != 2.0 {
                        return true;
                    }
                }
            } else if token.len() >= 3 {
                return true;
            }
        }
    }
    false
}

fn comment_markers(lang: Lang) -> &'static [&'static str] {
    match lang {
        Lang::Python => &["#"],
        // `*` and `*/` carry the middle and end of a doc comment, which is where
        // most of the explaining in a TypeScript project actually lives.
        _ => &["//", "/*", "*"],
    }
}

fn is_comment_line(trimmed: &str, lang: Lang) -> bool {
    comment_markers(lang).iter().any(|marker| trimmed.starts_with(marker))
}

fn strip_markers(trimmed: &str, _lang: Lang) -> String {
    // The end of a doc comment comes off first: trimming `*` from `*/` would
    // leave a stray slash on the end of the draft.
    let mut text = trimmed.trim().trim_end_matches("*/").trim_end();
    for marker in ["/**", "/*", "//", "*", "#"] {
        if let Some(rest) = text.strip_prefix(marker) {
            text = rest.trim_start();
            break;
        }
    }
    text.trim().to_string()
}

/// Code only: string bodies and comments removed, line count preserved.
pub fn code_only(text: &str, lang: Lang) -> String {
    let mut out = String::with_capacity(text.len());
    for line in text.split('\n') {
        let mut quote: Option<char> = None;
        let mut chars = line.chars().peekable();
        while let Some(c) = chars.next() {
            match quote {
                Some(open) => {
                    if c == '\\' {
                        chars.next();
                    } else if c == open {
                        quote = None;
                    }
                }
                None => {
                    if c == '"' || c == '\'' || c == '`' {
                        quote = Some(c);
                    } else if c == '#' && lang == Lang::Python {
                        break;
                    } else if c == '/' && lang != Lang::Python
                        && matches!(chars.peek(), Some('/') | Some('*'))
                    {
                        break;
                    } else {
                        out.push(c);
                    }
                }
            }
        }
        out.push('\n');
    }
    out
}

/// A statement's own lines, minus anything nested inside it.
///
/// A compound statement otherwise inherits every signal in its body, so a
/// function outranks the sleep three lines inside it and the useful suggestion
/// is buried.
pub fn own_text(candidate: &Candidate, siblings: &[Candidate], lines: &[&str], lang: Lang) -> String {
    let mut inside = vec![false; lines.len() + 2];
    for other in siblings {
        if other.line > candidate.line && other.end_line <= candidate.end_line {
            for number in other.line..=other.end_line.min(lines.len()) {
                inside[number] = true;
            }
        }
    }
    let kept: Vec<&str> = (candidate.line..=candidate.end_line.min(lines.len()))
        .filter(|number| !inside[*number])
        .map(|number| lines[number - 1])
        .collect();
    code_only(&kept.join("\n"), lang)
}

/// Everything a person wrote about this statement without being asked: the
/// comment block above it, and any comment at the end of its own first line.
pub fn comment_for(lines: &[&str], line: usize, lang: Lang) -> String {
    let mut collected: Vec<String> = Vec::new();
    let mut at = line.saturating_sub(1);
    while at >= 1 && collected.len() < COMMENT_LINES {
        let trimmed = lines[at - 1].trim();
        if !is_comment_line(trimmed, lang) {
            break;
        }
        let text = strip_markers(trimmed, lang);
        if !text.is_empty() {
            collected.push(text);
        }
        at -= 1;
        // `/*` opens the block, so there is nothing above it worth reading.
        if lang != Lang::Python && trimmed.starts_with("/*") {
            break;
        }
    }
    collected.reverse();

    if let Some(own) = lines.get(line - 1) {
        let trailing = trailing_comment(own, lang);
        if !trailing.is_empty() {
            collected.push(trailing);
        }
    }
    collected.join(" ").trim().to_string()
}

/// `setTimeout(fn, 50);  // safari fires focus twice` -- a reason lives here too.
///
/// Scanned rather than measured: taking the offset from a string-stripped copy
/// of the line would point at the wrong characters in the real one.
pub fn trailing_comment(line: &str, lang: Lang) -> String {
    let mut quote: Option<char> = None;
    let mut chars = line.char_indices().peekable();
    while let Some((at, c)) = chars.next() {
        match quote {
            Some(open) => {
                if c == '\\' {
                    chars.next();
                } else if c == open {
                    quote = None;
                }
            }
            None => {
                if c == '"' || c == '\'' || c == '`' {
                    quote = Some(c);
                } else if (c == '#' && lang == Lang::Python)
                    || (c == '/'
                        && lang != Lang::Python
                        && matches!(chars.peek(), Some((_, '/')) | Some((_, '*'))))
                {
                    return strip_markers(line[at..].trim(), lang);
                }
            }
        }
    }
    String::new()
}

pub fn rank(candidate: &Candidate, body: &str, comment: &str, copies_elsewhere: usize) -> Suggestion {
    let mut found = Suggestion {
        path: candidate.path.clone(),
        line: candidate.line,
        end_line: candidate.end_line,
        kind: candidate.kind.clone(),
        scope: anchor::scope_label(&candidate.scope).to_string(),
        text: candidate.snippet.lines().next().unwrap_or("").trim().to_string(),
        score: 0,
        reasons: Vec::new(),
        draft: None,
    };
    let body = body.to_lowercase();
    let said = comment.to_lowercase();

    if !said.is_empty() && mentions(&said, EXPLAINS) {
        found.score += 5;
        found.reasons.push(
            "a comment above it explains something, and a comment is deleted with the code it explains"
                .to_string(),
        );
        found.draft = Some(comment.to_string());
    } else if !said.is_empty() {
        found.score += 1;
        found
            .reasons
            .push("a comment above it, though it only says what the code says".to_string());
    }

    if mentions(&body, TIMING) {
        found.score += 3;
        found
            .reasons
            .push("names a timing primitive, and nobody picks a delay by choice".to_string());
    }
    let environmental = !said.is_empty() && mentions(&said, ENVIRONMENT);
    if mentions(&body, ENVIRONMENT) || environmental {
        found.score += 3;
        found.reasons.push(
            "names a browser, platform or wire format, so it answers to something outside this repository"
                .to_string(),
        );
        if environmental && found.draft.is_none() {
            found.draft = Some(comment.to_string());
        }
    }
    if candidate.kind.contains("try_statement") {
        found.score += 2;
        found
            .reasons
            .push("catches something, and which failure it forgives is rarely obvious".to_string());
    }
    if chosen_number(&body) {
        found.score += 2;
        found.reasons.push("contains a number somebody chose".to_string());
    }

    if copies_elsewhere >= CONVENTION {
        found.score -= 4;
        found.reasons.push(format!(
            "written the same way in {copies_elsewhere} other places, so it is a convention rather than a decision"
        ));
    }
    if is_test(&candidate.path) {
        found.score -= 2;
        found
            .reasons
            .push("a test, where a value is often arbitrary on purpose".to_string());
    }
    if is_plumbing(&candidate.kind) {
        found.score -= 3;
    }
    found
}

fn is_test(path: &str) -> bool {
    let lower = path.to_lowercase();
    lower.contains("/tests/")
        || lower.contains("/test/")
        || lower.starts_with("tests/")
        || lower.starts_with("test/")
        || lower.contains("/test_")
        || lower.starts_with("test_")
        || lower.contains(".test.")
        || lower.contains(".spec.")
}

fn is_plumbing(kind: &str) -> bool {
    matches!(
        kind,
        "import_statement"
            | "import_from_statement"
            | "future_import_statement"
            | "expression_statement.string"
            | "pass_statement"
            | "global_statement"
            | "nonlocal_statement"
            | "empty_statement"
    )
}

/// Rank every statement nobody has recorded a reason for yet.
pub fn collect(
    index: &Index,
    paths: &[String],
    taken: &[(String, usize)],
    limit: usize,
    read: impl Fn(&str) -> Option<String>,
) -> Vec<Suggestion> {
    let mut elsewhere: HashMap<String, usize> = HashMap::new();
    let mut here: HashMap<(&str, &str), usize> = HashMap::new();
    for path in paths {
        for candidate in index.get(path) {
            *elsewhere.entry(candidate.exact.clone()).or_insert(0) += 1;
            *here.entry((path.as_str(), candidate.exact.as_str())).or_insert(0) += 1;
        }
    }

    let mut found = Vec::new();
    for path in paths {
        let candidates = index.get(path);
        if candidates.is_empty() {
            continue;
        }
        let lang = match Lang::of(path) {
            Some(lang) => lang,
            None => continue,
        };
        let source = match read(path) {
            Some(source) => source,
            None => continue,
        };
        let lines: Vec<&str> = source.split('\n').collect();
        for candidate in candidates {
            if taken.iter().any(|(p, line)| p == path && *line == candidate.line) {
                continue;
            }
            let total = elsewhere.get(&candidate.exact).copied().unwrap_or(0);
            let mine = here
                .get(&(path.as_str(), candidate.exact.as_str()))
                .copied()
                .unwrap_or(0);
            let scored = rank(
                candidate,
                &own_text(candidate, candidates, &lines, lang),
                &comment_for(&lines, candidate.line, lang),
                total.saturating_sub(mine),
            );
            if scored.score > 0 {
                found.push(scored);
            }
        }
    }
    found.sort_by(|a, b| {
        b.score
            .cmp(&a.score)
            .then(a.path.cmp(&b.path))
            .then(a.line.cmp(&b.line))
    });
    found.truncate(limit);
    found
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn words_match_whole_words_only() {
        assert!(mentions("it names a sleep here", TIMING));
        assert!(!mentions("asleep at the wheel", TIMING));
        assert!(mentions("kept so that it works", EXPLAINS));
    }

    #[test]
    fn structure_is_not_a_chosen_number() {
        assert!(!chosen_number("similarity: number = 1.0"));
        assert!(!chosen_number("const index = 0"));
        assert!(chosen_number("setTimeout(fn, 0.05)"));
        assert!(chosen_number("const port = 8420"));
    }

    #[test]
    fn a_doc_comment_comes_out_as_a_sentence() {
        let source = [
            "/**",
            " * Shared rather than per-instance on purpose: one upstream fetch per",
            " * TTL window, which is what keeps this inside free-tier caps.",
            " */",
            "export async function cacheGet() {}",
        ];
        let said = comment_for(&source, 5, Lang::TypeScript);
        assert!(said.starts_with("Shared rather than per-instance"), "{said}");
        assert!(said.ends_with("free-tier caps."), "{said}");
        assert!(!said.contains('*'), "{said}");
        assert!(!said.contains('/'), "{said}");
    }

    #[test]
    fn strings_and_comments_are_not_the_code_speaking() {
        let text = code_only("const a = \"safari\"; // windows\n", Lang::Tsx);
        assert!(!text.contains("safari"));
        assert!(!text.contains("windows"));
        assert!(text.contains("const a"));
    }
}

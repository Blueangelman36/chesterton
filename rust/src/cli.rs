//! fence: remember why code exists, and speak up before it's removed.

use std::collections::{BTreeMap, HashSet};
use std::path::{Path, PathBuf};

use serde_json::json;

use crate::anchor::{self, Candidate, How, Index, Match};
use crate::git::{self, Error, Staged, Worktree};
use crate::store::{self, Note, Store};
use crate::text;

const HOOK_MARKER: &str = "# installed by fence";

pub fn main() -> i32 {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    match run(&argv) {
        Ok(code) => code,
        Err(e) => {
            eprintln!("fence: {e}");
            2
        }
    }
}

fn run(argv: &[String]) -> Result<i32, Error> {
    let (command, rest) = match argv.split_first() {
        Some((first, rest)) => (first.as_str(), rest),
        None => {
            print!("{}", text::USAGE);
            return Ok(2);
        }
    };
    let args = Args::parse(rest);
    match command {
        "init" => cmd_init(&args),
        "why" => cmd_why(&args),
        "add" => cmd_add(&args),
        "list" => cmd_list(&args),
        "check" => cmd_check(&args),
        "update" => cmd_update(&args),
        "confirm" => cmd_confirm(&args),
        "reanchor" => cmd_reanchor(&args),
        "retire" => cmd_retire(&args),
        "help" | "-h" | "--help" => {
            print!("{}", text::USAGE);
            Ok(0)
        }
        other => Err(Error(format!("unknown command {other:?}; try `fence --help`"))),
    }
}

struct Args {
    positional: Vec<String>,
    flags: HashSet<String>,
    values: BTreeMap<String, String>,
}

impl Args {
    fn parse(argv: &[String]) -> Self {
        let mut positional = Vec::new();
        let mut flags = HashSet::new();
        let mut values = BTreeMap::new();
        let mut index = 0;
        while index < argv.len() {
            let item = argv[index].as_str();
            let takes_value = match item {
                "-m" | "--message" => Some("message"),
                "--source" => Some("source"),
                _ => None,
            };
            if let Some(key) = takes_value {
                if let Some(value) = argv.get(index + 1) {
                    values.insert(key.to_string(), value.clone());
                    index += 2;
                    continue;
                }
                index += 1;
                continue;
            }
            if let Some(name) = item.strip_prefix("--") {
                flags.insert(name.to_string());
            } else if item.starts_with('-') && item.len() > 1 {
                flags.insert(item.trim_start_matches('-').to_string());
            } else {
                positional.push(item.to_string());
            }
            index += 1;
        }
        Args { positional, flags, values }
    }

    fn has(&self, name: &str) -> bool {
        self.flags.contains(name)
    }

    fn value(&self, name: &str) -> Option<&str> {
        self.values.get(name).map(String::as_str)
    }

    fn at(&self, position: usize) -> Option<&str> {
        self.positional.get(position).map(String::as_str)
    }

    fn required(&self, position: usize, what: &str) -> Result<&str, Error> {
        self.at(position).ok_or_else(|| Error(format!("expected {what}")))
    }
}

fn cmd_init(args: &Args) -> Result<i32, Error> {
    let root = git::repo_root()?;
    let store = Store::new(&root);
    std::fs::create_dir_all(&store.dir)?;
    explain(&store.dir)?;

    let hook = git::hooks_dir(&root)?.join("pre-commit");
    let command = hook_command()?;
    if hook.exists() {
        let existing = std::fs::read_to_string(&hook).unwrap_or_default();
        if !existing.contains(HOOK_MARKER) {
            println!(
                "fence: {} already exists. Add this line to it to enable fence:\n  {command}",
                hook.display()
            );
            return Ok(1);
        }
    }
    if let Some(parent) = hook.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(
        &hook,
        format!(
            "#!/bin/sh\n{HOOK_MARKER}\n# Blocks commits that delete code with a recorded reason.\n{command}\n"
        ),
    )?;
    make_executable(&hook);
    println!(
        "fence: installed pre-commit hook. Record a reason with: fence add <file>:<line> -m \"why\""
    );
    if args.has("agents") {
        for written in install_agent_docs(&root)? {
            println!("fence: wrote {written}");
        }
    } else {
        println!("fence: `fence init --agents` also tells coding agents the notes are here.");
    }
    Ok(0)
}

fn cmd_add(args: &Args) -> Result<i32, Error> {
    let root = git::repo_root()?;
    let (path, start, end) = parse_loc(args.required(0, "<file>:<line>")?, &root)?;
    let (index, target) = pick(&root, &path, start, end)?;
    let store = Store::new(&root);
    if !args.has("also") {
        refuse_duplicate(&store, &index, &target, &path)?;
    }

    let mut reason = args.value("message").map(str::to_string);
    let mut source = args.value("source").map(str::to_string);
    if args.has("from-blame") {
        let (sha, message) = git::blame(&root, &path, start)?;
        if reason.is_none() {
            reason = Some(message);
        }
        if source.is_none() {
            source = Some(format!("commit {}", &sha[..sha.len().min(12)]));
        }
    }
    let reason = reason
        .map(|text| text.trim().to_string())
        .filter(|text| !text.is_empty())
        .ok_or_else(|| {
            Error(
                "say why with -m \"...\", or use --from-blame to borrow the message of the commit that wrote the line"
                    .to_string(),
            )
        })?;

    let note = Note {
        id: store::new_id(&path, target.line, &reason),
        reason,
        source,
        author: git::config(&root, "user.name"),
        created: store::today(),
        snippet: target.snippet.clone(),
        anchor: anchor::make_anchor(&target, index.get(&path), &index.others(&path)),
        retired: None,
        retired_because: None,
    };
    store.save(&note)?;
    git::stage_notes(&root)?;
    println!(
        "fence: added {}  {}:{}  in {}",
        note.id,
        path,
        target.line,
        anchor::scope_label(&target.scope)
    );
    println!("    {}", first_line(&target.snippet));
    Ok(0)
}

fn cmd_why(args: &Args) -> Result<i32, Error> {
    let root = git::repo_root()?;
    let (target, start, end) = parse_target(args.required(0, "<file>[:<line>]")?, &root)?;
    let store = Store::new(&root);
    let index = Index::scan(&Worktree { root: root.clone() });

    let mut found = Vec::new();
    for note in store.notes()? {
        if !under(&note.anchor.path, &target) {
            continue;
        }
        let located = anchor::locate(&note.anchor, &index);
        let (first, last) = match &located.candidate {
            Some(c) => (c.line, c.end_line),
            None => (note.anchor.line, note.anchor.line),
        };
        if let Some(from) = start {
            let to = end.unwrap_or(from);
            if last < from || first > to {
                continue;
            }
        }
        found.push((note, located));
    }

    if args.has("json") {
        let notes: Vec<serde_json::Value> =
            found.iter().map(|(note, m)| as_json(note, m)).collect();
        println!("{}", serde_json::to_string_pretty(&json!({"notes": notes})).unwrap_or_default());
        return Ok(0);
    }
    if found.is_empty() {
        match start {
            Some(line) => println!("fence: nothing recorded for {target}:{line}"),
            None => println!("fence: nothing recorded for {target}"),
        }
        return Ok(0);
    }
    for (note, located) in &found {
        let (path, line, scope) = match &located.candidate {
            Some(c) => (c.path.clone(), c.line, c.scope.clone()),
            None => (note.anchor.path.clone(), note.anchor.line, note.anchor.scope.clone()),
        };
        println!(
            "{path}:{line}  in {}  [{}]  {}",
            anchor::scope_label(&scope),
            label(&located.how),
            note.id
        );
        println!("{}", block(&note.reason, "why: "));
        if let Some(source) = &note.source {
            println!("{}", block(source, "from:"));
        }
        println!("    code: {}", first_line(&note.snippet));
    }
    Ok(0)
}

fn cmd_list(args: &Args) -> Result<i32, Error> {
    let root = git::repo_root()?;
    let mut notes = Store::new(&root).notes()?;
    if let Some(path) = args.at(0) {
        let prefix = repo_path(path, &root)?;
        notes.retain(|note| under(&note.anchor.path, &prefix));
    }
    if notes.is_empty() {
        println!("fence: no notes yet. Add one with: fence add <file>:<line> -m \"why\"");
    }
    for note in &notes {
        println!(
            "{}  {}:{}  in {}",
            note.id,
            note.anchor.path,
            note.anchor.line,
            anchor::scope_label(&note.anchor.scope)
        );
        println!("    why:  {}", first_line(&note.reason));
        if let Some(source) = &note.source {
            println!("    from: {source}");
        }
    }
    Ok(0)
}

fn cmd_check(args: &Args) -> Result<i32, Error> {
    let root = git::repo_root()?;
    let store = Store::new(&root);
    let mut notes = store.notes()?;
    let staged = args.has("staged");

    let results: Vec<(Note, Match)> = if staged {
        // Only what this commit touches: never nag about code nobody is editing.
        let touched: HashSet<String> = git::staged_paths(&root)?.into_iter().collect();
        notes.retain(|note| touched.contains(&note.anchor.path));
        let source = Staged { root: root.clone() };
        let listed: Vec<String> = touched.iter().cloned().collect();
        let near = Index::scan_paths(&source, &listed);
        let mut first_pass: Vec<(Note, Match)> = notes
            .into_iter()
            .map(|note| {
                let located = anchor::locate(&note.anchor, &near);
                (note, located)
            })
            .collect();
        // Before saying code is gone, look further than the files in the commit:
        // it may have moved into one of them from somewhere else.
        if first_pass.iter().any(|(_, m)| m.how == How::Removed) {
            let everything = Index::scan(&source);
            for (note, located) in first_pass.iter_mut() {
                if located.how == How::Removed {
                    *located = anchor::locate(&note.anchor, &everything);
                }
            }
        }
        first_pass
    } else {
        let index = Index::scan(&Worktree { root: root.clone() });
        notes
            .into_iter()
            .map(|note| {
                let located = anchor::locate(&note.anchor, &index);
                (note, located)
            })
            .collect()
    };

    let removed = results.iter().filter(|(_, m)| m.how == How::Removed).count();
    if args.has("json") {
        let notes: Vec<serde_json::Value> =
            results.iter().map(|(note, m)| as_json(note, m)).collect();
        let payload = json!({
            "checked": results.len(),
            "counts": counts(&results),
            "blocked": removed > 0,
            "notes": notes,
        });
        println!("{}", serde_json::to_string_pretty(&payload).unwrap_or_default());
        return Ok(if removed > 0 { 1 } else { 0 });
    }

    let shown: Vec<&(Note, Match)> = results
        .iter()
        .filter(|(_, m)| !(staged && m.how == How::Ok))
        .collect();
    for (note, located) in &shown {
        println!("{}", describe(note, located));
    }
    if !staged || !shown.is_empty() {
        let tally: Vec<String> = counts(&results)
            .iter()
            .map(|(name, count)| format!("{count} {name}"))
            .collect();
        let summary = if tally.is_empty() {
            String::new()
        } else {
            format!(": {}", tally.join(", "))
        };
        println!("\nfence: {} note(s) checked{summary}", results.len());
        for hint in hints(&results, staged) {
            println!("  {hint}");
        }
    }
    Ok(if removed > 0 { 1 } else { 0 })
}

fn cmd_update(_args: &Args) -> Result<i32, Error> {
    let root = git::repo_root()?;
    let store = Store::new(&root);
    let index = Index::scan(&Worktree { root: root.clone() });
    let mut updated = 0;
    for mut note in store.notes()? {
        let located = anchor::locate(&note.anchor, &index);
        if !matches!(located.how, How::Ok | How::Renamed | How::Moved) {
            continue;
        }
        let how = located.how.clone();
        let candidate = match located.candidate {
            Some(candidate) => candidate,
            None => continue,
        };
        if repin(&store, &mut note, &candidate, &index)? {
            updated += 1;
            if how != How::Ok {
                println!(
                    "fence: {} now follows {}:{}  in {}",
                    note.id,
                    candidate.path,
                    candidate.line,
                    anchor::scope_label(&candidate.scope)
                );
            }
        }
    }
    if updated > 0 {
        git::stage_notes(&root)?;
    }
    println!("fence: refreshed {updated} note(s)");
    Ok(0)
}

fn cmd_confirm(args: &Args) -> Result<i32, Error> {
    let root = git::repo_root()?;
    let store = Store::new(&root);
    let mut note = store.get(args.required(0, "a note id")?)?;
    let index = Index::scan(&Worktree { root: root.clone() });
    let candidate = anchor::locate(&note.anchor, &index).candidate.ok_or_else(|| {
        Error(format!(
            "can't find the code for {}; point at it with: fence reanchor {} <file>:<line>",
            note.id, note.id
        ))
    })?;
    repin(&store, &mut note, &candidate, &index)?;
    git::stage_notes(&root)?;
    println!("fence: {} confirmed at {}:{}", note.id, candidate.path, candidate.line);
    Ok(0)
}

fn cmd_reanchor(args: &Args) -> Result<i32, Error> {
    let root = git::repo_root()?;
    let store = Store::new(&root);
    let mut note = store.get(args.required(0, "a note id")?)?;
    let (path, start, end) = parse_loc(args.required(1, "<file>:<line>")?, &root)?;
    let (index, target) = pick(&root, &path, start, end)?;
    repin(&store, &mut note, &target, &index)?;
    git::stage_notes(&root)?;
    println!(
        "fence: {} now points at {}:{}  in {}",
        note.id,
        path,
        target.line,
        anchor::scope_label(&target.scope)
    );
    println!("    {}", first_line(&target.snippet));
    Ok(0)
}

fn cmd_retire(args: &Args) -> Result<i32, Error> {
    let root = git::repo_root()?;
    let store = Store::new(&root);
    let note = store.get(args.required(0, "a note id")?)?;
    let why = args
        .value("message")
        .ok_or_else(|| Error("say what changed with -m \"...\"".to_string()))?;
    store.retire(&note, why.trim())?;
    git::stage_notes(&root)?;
    println!("fence: retired {} (kept in .fence/retired/)", note.id);
    Ok(0)
}

fn pick(root: &Path, path: &str, start: usize, end: usize) -> Result<(Index, Candidate), Error> {
    let worktree = Worktree { root: root.to_path_buf() };
    let text = anchor::Source::read(&worktree, path)
        .ok_or_else(|| Error(format!("{path}: no such file")))?;
    let candidates =
        anchor::candidates(path, &text).ok_or_else(|| Error(format!("{path} doesn't parse")))?;
    let target = anchor::pick(&candidates, start, end)
        .ok_or_else(|| {
            let span = if end == start { start.to_string() } else { format!("{start}-{end}") };
            Error(format!("no statement covers {path}:{span}"))
        })?
        .clone();
    Ok((Index::scan(&worktree), target))
}

/// Two notes on one statement is nearly always a re-run, and occasionally meant.
fn refuse_duplicate(
    store: &Store,
    index: &Index,
    target: &Candidate,
    path: &str,
) -> Result<(), Error> {
    let mut here = Vec::new();
    for note in store.notes()? {
        if note.anchor.path != target.path && note.anchor.exact != target.exact {
            continue;
        }
        if let Some(found) = anchor::locate(&note.anchor, index).candidate {
            if found.path == target.path && found.line == target.line && found.exact == target.exact
            {
                here.push(note);
            }
        }
    }
    if here.is_empty() {
        return Ok(());
    }
    let listed: Vec<String> = here
        .iter()
        .map(|note| format!("{}  {}", note.id, first_line(&note.reason)))
        .collect();
    Err(Error(format!(
        "{path}:{} already has a reason recorded:\n  {}\n  --also records a second, independent reason for the same code",
        target.line,
        listed.join("\n  ")
    )))
}

fn repin(
    store: &Store,
    note: &mut Note,
    target: &Candidate,
    index: &Index,
) -> Result<bool, Error> {
    let anchor = anchor::make_anchor(target, index.get(&target.path), &index.others(&target.path));
    if anchor == note.anchor && target.snippet == note.snippet {
        return Ok(false);
    }
    note.anchor = anchor;
    note.snippet = target.snippet.clone();
    store.save(note)?;
    Ok(true)
}

fn counts(results: &[(Note, Match)]) -> BTreeMap<String, usize> {
    let mut tally: BTreeMap<String, usize> = BTreeMap::new();
    for (_, located) in results {
        *tally.entry(label(&located.how).to_string()).or_insert(0) += 1;
    }
    tally
}

fn hints(results: &[(Note, Match)], staged: bool) -> Vec<String> {
    let seen = |wanted: How| results.iter().any(|(_, m)| m.how == wanted);
    let mut hints = Vec::new();
    if seen(How::Removed) {
        hints.push("reason no longer applies?    fence retire <id> -m \"what changed\"".to_string());
        hints.push("code lives somewhere new?    fence reanchor <id> <file>:<line>".to_string());
        if staged {
            hints.push("commit anyway?               git commit --no-verify".to_string());
        }
    }
    if seen(How::Changed) || seen(How::Ambiguous) {
        hints.push("reason still holds?          fence confirm <id>".to_string());
    }
    if seen(How::Renamed) || seen(How::Moved) {
        hints.push("follow code that moved:      fence update".to_string());
    }
    hints
}

fn describe(note: &Note, located: &Match) -> String {
    let tag = if located.how == How::Changed {
        format!("{} {:.0}%", label(&located.how), located.similarity * 100.0)
    } else {
        label(&located.how).to_string()
    };
    let place = match (&located.candidate, &located.how) {
        (Some(c), How::Moved) => format!(
            "{}:{} -> {}:{}  in {}",
            note.anchor.path,
            note.anchor.line,
            c.path,
            c.line,
            anchor::scope_label(&c.scope)
        ),
        (Some(c), _) => format!("{}:{}  in {}", c.path, c.line, anchor::scope_label(&c.scope)),
        (None, _) => format!(
            "{}:{}  in {}",
            note.anchor.path,
            note.anchor.line,
            anchor::scope_label(&note.anchor.scope)
        ),
    };
    let mut lines = vec![
        format!("[{tag}] {}  {place}", note.id),
        format!("    why: {}", first_line(&note.reason)),
    ];
    match located.how {
        How::Removed => lines.push(format!("    was: {}", first_line(&note.snippet))),
        How::Changed => {
            if let Some(c) = &located.candidate {
                let (was, now) = first_difference(&note.snippet, &c.snippet);
                lines.push(format!("    was: {was}"));
                lines.push(format!("    now: {now}"));
            }
        }
        How::Ambiguous => lines.push(
            "    an identical copy of this statement was removed; can't tell which one this note is about"
                .to_string(),
        ),
        How::Unparseable => lines.push(format!(
            "    {} doesn't parse right now, so this note wasn't checked",
            note.anchor.path
        )),
        _ => {}
    }
    lines.join("\n")
}

fn as_json(note: &Note, located: &Match) -> serde_json::Value {
    json!({
        "id": note.id,
        "status": located.how.as_str(),
        "reason": note.reason,
        "source": note.source,
        "author": note.author,
        "created": note.created,
        "similarity": if located.how == How::Changed {
            json!((located.similarity * 1000.0).round() / 1000.0)
        } else {
            serde_json::Value::Null
        },
        "recorded_at": {
            "path": note.anchor.path,
            "line": note.anchor.line,
            "scope": anchor::scope_label(&note.anchor.scope),
        },
        "found_at": located.candidate.as_ref().map(|c| json!({
            "path": c.path,
            "line": c.line,
            "end_line": c.end_line,
            "scope": anchor::scope_label(&c.scope),
        })),
        "snippet": note.snippet,
    })
}

fn label(how: &How) -> &'static str {
    match how {
        How::Ok => "ok",
        How::Renamed => "renamed",
        How::Moved => "moved",
        How::Changed => "changed",
        How::Ambiguous => "ambiguous",
        How::Removed => "REMOVED",
        How::Unparseable => "skipped",
    }
}

fn first_line(text: &str) -> String {
    text.trim().lines().next().unwrap_or("").to_string()
}

fn first_difference(was: &str, now: &str) -> (String, String) {
    for (left, right) in was.lines().zip(now.lines()) {
        if left.trim() != right.trim() {
            return (left.trim().to_string(), right.trim().to_string());
        }
    }
    (first_line(was), first_line(now))
}

fn block(text: &str, prefix: &str) -> String {
    let mut lines = text.trim().lines();
    let head = lines.next().unwrap_or("");
    let mut rendered = format!("    {prefix} {head}");
    let padding = " ".repeat(prefix.len());
    for line in lines {
        rendered.push_str(&format!("\n    {padding} {line}"));
    }
    rendered
}

fn parse_loc(loc: &str, root: &Path) -> Result<(String, usize, usize), Error> {
    let complaint =
        || Error(format!("expected <file>:<line> or <file>:<start>-<end>, got {loc:?}"));
    let (path, span) = loc.rsplit_once(':').ok_or_else(complaint)?;
    let (first, last) = span.split_once('-').unwrap_or((span, span));
    let start: usize = first.parse().map_err(|_| complaint())?;
    let end: usize = last.parse().map_err(|_| complaint())?;
    if path.is_empty() || start < 1 || end < start {
        return Err(complaint());
    }
    Ok((repo_path(path, root)?, start, end))
}

/// A file or directory, optionally narrowed to a line or a range.
fn parse_target(loc: &str, root: &Path) -> Result<(String, Option<usize>, Option<usize>), Error> {
    if let Some((_, span)) = loc.rsplit_once(':') {
        let numeric = !span.is_empty() && span.chars().all(|c| c.is_ascii_digit() || c == '-');
        if numeric {
            let (path, start, end) = parse_loc(loc, root)?;
            return Ok((path, Some(start), Some(end)));
        }
    }
    Ok((repo_path(loc, root)?, None, None))
}

fn repo_path(path: &str, root: &Path) -> Result<String, Error> {
    let here = std::env::current_dir()?;
    let full = if Path::new(path).is_absolute() {
        PathBuf::from(path)
    } else {
        here.join(path)
    };
    let full = full.canonicalize().unwrap_or(full);
    let root = root.canonicalize().unwrap_or_else(|_| root.to_path_buf());
    let relative = full
        .strip_prefix(&root)
        .map_err(|_| Error(format!("{path} is outside the repository")))?;
    Ok(relative.to_string_lossy().replace('\\', "/"))
}

fn under(path: &str, target: &str) -> bool {
    path == target || path.starts_with(&format!("{}/", target.trim_end_matches('/')))
}

fn hook_command() -> Result<String, Error> {
    let exe = std::env::current_exe()?;
    Ok(format!(
        "\"{}\" check --staged || exit 1",
        exe.display().to_string().replace('\\', "/")
    ))
}

/// Leave an explanation for whoever meets .fence in a pull request cold.
fn explain(fence_dir: &Path) -> Result<(), Error> {
    let readme = fence_dir.join("README.md");
    if !readme.exists() {
        std::fs::write(&readme, text::FENCE_README)?;
    }
    let ignore = fence_dir.join(".gitignore");
    if !ignore.exists() {
        std::fs::write(&ignore, "cache.json\ncache.json.tmp\n")?;
    }
    Ok(())
}

fn install_agent_docs(root: &Path) -> Result<Vec<String>, Error> {
    let mut written = Vec::new();
    let agents = root.join("AGENTS.md");
    let existing = std::fs::read_to_string(&agents).unwrap_or_default();
    if !existing.contains(text::AGENTS_MARKER) {
        let separator = if existing.trim().is_empty() { "" } else { "\n\n" };
        let combined = format!("{}{separator}{}", existing.trim_end(), text::AGENTS_BLOCK);
        std::fs::write(&agents, combined)?;
        written.push("AGENTS.md".to_string());
    }
    let skill = root.join(".claude").join("skills").join("fence").join("SKILL.md");
    if !skill.exists() {
        if let Some(parent) = skill.parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::write(&skill, text::SKILL)?;
        written.push(".claude/skills/fence/SKILL.md".to_string());
    }
    Ok(written)
}

#[cfg(unix)]
fn make_executable(path: &Path) {
    use std::os::unix::fs::PermissionsExt;
    let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o755));
}

#[cfg(not(unix))]
fn make_executable(_path: &Path) {}

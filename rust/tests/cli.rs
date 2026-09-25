//! End to end: a real git repository, the real hook, the real binary.

use std::path::PathBuf;
use std::process::Command;

const APP: &str = r#"class Client:
    def fetch(self, url):
        resp = self.session.get(url, timeout=10)
        if resp.json().get("error") == "unauthorized":
            raise AuthError(url)
        return resp
"#;

const GUARD: &str = "        if resp.json().get(\"error\") == \"unauthorized\":\n            raise AuthError(url)\n";
const REASON: &str = "Vendor API returns 200 on auth failure; the error is in the body.";

struct Repo {
    dir: PathBuf,
}

impl Repo {
    fn new(name: &str) -> Repo {
        let dir = std::env::temp_dir().join(format!("fence-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("could not make a temporary repository");
        let repo = Repo { dir };
        repo.git(&["init", "-q"]);
        repo.git(&["config", "user.name", "Test"]);
        repo.git(&["config", "user.email", "test@example.com"]);
        repo.git(&["config", "commit.gpgsign", "false"]);
        repo.write("app.py", APP);
        repo.git(&["add", "app.py"]);
        repo.git(&["commit", "-q", "-m", "first"]);
        repo
    }

    fn write(&self, name: &str, text: &str) {
        std::fs::write(self.dir.join(name), text).expect("could not write a file");
    }

    fn read(&self, name: &str) -> String {
        std::fs::read_to_string(self.dir.join(name)).expect("could not read a file")
    }

    fn git(&self, args: &[&str]) -> String {
        let output = Command::new("git")
            .args(args)
            .current_dir(&self.dir)
            .output()
            .expect("could not run git");
        format!(
            "{}{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        )
    }

    fn commit(&self, message: &str) -> (i32, String) {
        self.git(&["add", "-A"]);
        let output = Command::new("git")
            .args(["commit", "-q", "-m", message])
            .current_dir(&self.dir)
            .output()
            .expect("could not run git");
        (
            output.status.code().unwrap_or(-1),
            format!(
                "{}{}",
                String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr)
            ),
        )
    }

    fn fence(&self, args: &[&str]) -> (i32, String) {
        let output = Command::new(env!("CARGO_BIN_EXE_fence"))
            .args(args)
            .current_dir(&self.dir)
            .output()
            .expect("could not run fence");
        (
            output.status.code().unwrap_or(-1),
            format!(
                "{}{}",
                String::from_utf8_lossy(&output.stdout),
                String::from_utf8_lossy(&output.stderr)
            ),
        )
    }

    fn note_id(&self) -> String {
        let notes = std::fs::read_dir(self.dir.join(".fence").join("notes"))
            .expect("no notes directory");
        for entry in notes.flatten() {
            let path = entry.path();
            if path.extension().and_then(|e| e.to_str()) == Some("json") {
                return path.file_stem().unwrap().to_string_lossy().to_string();
            }
        }
        panic!("no note was written");
    }
}

impl Drop for Repo {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

fn record(repo: &Repo) -> String {
    let (code, out) = repo.fence(&["init"]);
    assert_eq!(code, 0, "{out}");
    let (code, out) = repo.fence(&["add", "app.py:4", "-m", REASON]);
    assert_eq!(code, 0, "{out}");
    repo.note_id()
}

#[test]
fn a_reason_is_recorded_and_found_again() {
    let repo = Repo::new("record");
    let id = record(&repo);

    let (code, out) = repo.fence(&["check"]);
    assert_eq!(code, 0, "{out}");
    assert!(out.contains("[ok]"), "{out}");
    assert!(out.contains(&id), "{out}");

    let (code, out) = repo.fence(&["why", "app.py", "--json"]);
    assert_eq!(code, 0, "{out}");
    assert!(out.contains(REASON), "{out}");
    assert!(out.contains("\"status\": \"ok\""), "{out}");
}

#[test]
fn the_hook_blocks_a_commit_that_deletes_reasoned_code() {
    let repo = Repo::new("blocked");
    record(&repo);
    repo.git(&["add", "-A"]);
    repo.git(&["commit", "-q", "-m", "record the reason"]);

    let without_guard = repo.read("app.py").replace(GUARD, "");
    repo.write("app.py", &without_guard);

    let (code, out) = repo.commit("simplify fetch");
    assert_ne!(code, 0, "the commit should have been blocked: {out}");
    assert!(out.contains("REMOVED"), "{out}");
    assert!(out.contains(REASON), "{out}");
}

#[test]
fn retiring_the_reason_unblocks_the_commit() {
    let repo = Repo::new("retire");
    let id = record(&repo);
    repo.git(&["add", "-A"]);
    repo.git(&["commit", "-q", "-m", "record the reason"]);

    let without_guard = repo.read("app.py").replace(GUARD, "");
    repo.write("app.py", &without_guard);

    let (code, out) = repo.fence(&["retire", &id, "-m", "vendor fixed their status codes"]);
    assert_eq!(code, 0, "{out}");
    let (code, out) = repo.commit("simplify fetch");
    assert_eq!(code, 0, "{out}");
    assert!(repo.dir.join(".fence").join("retired").join(format!("{id}.json")).exists());
}

#[test]
fn a_note_from_another_build_is_adopted_rather_than_taken_over() {
    let repo = Repo::new("adopt");
    let id = record(&repo);
    let stored = repo.dir.join(".fence").join("notes").join(format!("{id}.json"));

    let mut written: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&stored).unwrap()).unwrap();
    let ours = written["anchors"]
        .as_object()
        .unwrap()
        .values()
        .next()
        .unwrap()
        .clone();
    let mut theirs = ours.clone();
    theirs["version"] = serde_json::json!(99);
    theirs["exact"] = serde_json::json!("not-comparable");
    theirs["shape"] = serde_json::json!("not-comparable");
    written["anchors"] = serde_json::json!({ "99": theirs });
    std::fs::write(&stored, serde_json::to_string_pretty(&written).unwrap()).unwrap();

    let (code, out) = repo.fence(&["check"]);
    assert_eq!(code, 0, "{out}");
    assert!(out.contains("other scheme"), "{out}");

    let (code, out) = repo.fence(&["update"]);
    assert_eq!(code, 0, "{out}");
    let kept: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&stored).unwrap()).unwrap();
    let mine = fence::anchor::ANCHOR_VERSION.to_string();
    assert!(kept["anchors"]["99"].is_object(), "{kept}");
    assert!(kept["anchors"][mine.as_str()].is_object(), "{kept}");
    let (_, out) = repo.fence(&["check"]);
    assert!(out.contains("[ok]"), "{out}");
}

#[test]
fn strict_fails_when_no_note_could_be_compared() {
    let repo = Repo::new("strict");
    let id = record(&repo);
    let stored = repo.dir.join(".fence").join("notes").join(format!("{id}.json"));
    let mut written: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&stored).unwrap()).unwrap();
    let mut theirs = written["anchors"].as_object().unwrap().values().next().unwrap().clone();
    theirs["version"] = serde_json::json!(99);
    theirs["exact"] = serde_json::json!("x");
    theirs["shape"] = serde_json::json!("x");
    written["anchors"] = serde_json::json!({ "99": theirs });
    std::fs::write(&stored, serde_json::to_string_pretty(&written).unwrap()).unwrap();

    let (code, out) = repo.fence(&["check"]);
    assert_eq!(code, 0, "{out}");
    assert!(out.contains("nothing is being guarded"), "{out}");

    let (code, out) = repo.fence(&["check", "--strict"]);
    assert_eq!(code, 1, "{out}");
    let (code, out) = repo.fence(&["check", "--strict", "--json"]);
    assert_eq!(code, 1, "{out}");
    let payload: serde_json::Value = serde_json::from_str(&out).unwrap();
    assert_eq!(payload["uncompared"], serde_json::json!(1));
    assert_eq!(payload["blocked"], serde_json::json!(true));

    repo.fence(&["update"]);
    let (code, out) = repo.fence(&["check", "--strict"]);
    assert_eq!(code, 0, "{out}");
}

/// Make the note look as though another build wrote it: one anchor, in `scheme`.
fn rescheme(repo: &Repo, id: &str, scheme: u32, path: Option<&str>) {
    let stored = repo.dir.join(".fence").join("notes").join(format!("{id}.json"));
    let mut written: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(&stored).unwrap()).unwrap();
    let mut theirs = written["anchors"].as_object().unwrap().values().next().unwrap().clone();
    theirs["version"] = serde_json::json!(scheme);
    theirs["exact"] = serde_json::json!("x");
    theirs["shape"] = serde_json::json!("x");
    if let Some(path) = path {
        theirs["path"] = serde_json::json!(path);
    }
    written["anchors"] = serde_json::json!({ scheme.to_string(): theirs });
    std::fs::write(&stored, serde_json::to_string_pretty(&written).unwrap()).unwrap();
}

fn doctor(repo: &Repo) -> (i32, serde_json::Value) {
    let (code, out) = repo.fence(&["doctor", "--json"]);
    (code, serde_json::from_str(&out).unwrap_or_else(|e| panic!("{e}: {out}")))
}

#[test]
fn doctor_approves_the_build_that_wrote_the_notes() {
    let repo = Repo::new("doctor-ok");
    record(&repo);
    let (code, out) = repo.fence(&["doctor"]);
    assert_eq!(code, 0, "{out}");
    assert!(out.contains("This build checks every note"), "{out}");
    assert!(out.contains("`implementation: rust`"), "{out}");
}

#[test]
fn doctor_names_the_build_the_notes_need() {
    // The case that stays green in CI while guarding nothing: every note is in a
    // scheme only the other build writes.
    let repo = Repo::new("doctor-other");
    let id = record(&repo);
    rescheme(&repo, &id, 3, None);
    let (code, report) = doctor(&repo);
    assert_eq!(code, 1);
    assert_eq!(report["complete"], serde_json::json!(false));
    assert_eq!(
        report["recommend"],
        serde_json::json!({ "implementation": "python", "update_first": false })
    );
    assert_eq!(
        report["schemes"],
        serde_json::json!([{ "scheme": 3, "notes": 1, "build": "python" }])
    );
    let mine = &report["builds"][0];
    assert_eq!(mine["implementation"], serde_json::json!("rust"));
    assert_eq!((mine["now"].as_u64(), mine["after_update"].as_u64()), (Some(0), Some(1)));

    let (_, out) = repo.fence(&["doctor"]);
    assert!(out.contains("Use the python build"), "{out}");
    assert!(out.contains("`fence check --strict` with it fails"), "{out}");
}

#[test]
fn doctor_says_which_files_a_build_cannot_read() {
    let repo = Repo::new("doctor-unread");
    let id = record(&repo);
    rescheme(&repo, &id, 5, Some("src/Client.kt"));
    let (code, report) = doctor(&repo);
    assert_eq!(code, 0);
    let python = report["builds"]
        .as_array()
        .unwrap()
        .iter()
        .find(|b| b["implementation"] == "python")
        .unwrap();
    assert_eq!(python["cannot_read"], serde_json::json!([".kt"]));
    assert_eq!(python["after_update"], serde_json::json!(0));
}

#[test]
fn doctor_sends_a_retired_scheme_to_fence_update() {
    let repo = Repo::new("doctor-retired");
    let id = record(&repo);
    rescheme(&repo, &id, 4, None);
    let (code, report) = doctor(&repo);
    assert_eq!(code, 1);
    assert_eq!(report["schemes"][0]["build"], serde_json::json!("retired"));
    assert_eq!(
        report["recommend"],
        serde_json::json!({ "implementation": "rust", "update_first": true })
    );
    let (_, out) = repo.fence(&["doctor"]);
    assert!(out.contains("Run `fence update` with the rust build"), "{out}");

    repo.fence(&["update"]);
    assert_eq!(doctor(&repo).0, 0);
}

#[test]
fn doctor_does_not_guess_at_a_scheme_it_has_never_heard_of() {
    let repo = Repo::new("doctor-unknown");
    let id = record(&repo);
    rescheme(&repo, &id, 99, None);
    let (code, report) = doctor(&repo);
    assert_eq!(code, 1);
    assert_eq!(report["schemes"][0]["build"], serde_json::json!("unknown"));
    let (_, out) = repo.fence(&["doctor"]);
    assert!(out.contains("a newer fence?"), "{out}");
    let (_, out) = repo.fence(&["check", "--strict"]);
    assert!(out.contains("fence doctor"), "{out}");
}

#[test]
fn doctor_with_no_notes() {
    let repo = Repo::new("doctor-empty");
    let id = record(&repo);
    repo.fence(&["retire", &id, "-m", "the vendor fixed it"]);
    let (code, out) = repo.fence(&["doctor"]);
    assert_eq!(code, 0, "{out}");
    assert!(out.contains("No notes yet"), "{out}");
}

#[test]
fn suggest_finds_a_typescript_statement_worth_recording() {
    let repo = Repo::new("suggest");
    repo.write(
        "widget.ts",
        "export function focus(el: HTMLElement): void {\n  \
         // Safari fires focus twice within 50ms, so the first one is swallowed deliberately.\n  \
         setTimeout(() => el.focus(), 50);\n}\n",
    );
    let (code, out) = repo.fence(&["suggest", "--json"]);
    assert_eq!(code, 0, "{out}");
    assert!(out.contains("widget.ts"), "{out}");
    assert!(out.contains("Safari"), "{out}");
    assert!(out.contains("setTimeout"), "{out}");
}

#[test]
fn statements_lists_what_a_note_could_be_pinned_to() {
    let repo = Repo::new("statements");
    let (code, out) = repo.fence(&["statements", "app.py", "--json"]);
    assert_eq!(code, 0, "{out}");
    assert!(out.contains("\"kind\": \"if_statement\""), "{out}");
    assert!(out.contains("\"scope\": \"Client.fetch\""), "{out}");
    assert!(out.contains("\"exact\""), "{out}");
}

#[test]
fn a_rename_is_followed_rather_than_reported() {
    let repo = Repo::new("rename");
    record(&repo);
    repo.write("app.py", &APP.replace("resp", "response"));

    let (code, out) = repo.fence(&["check"]);
    assert_eq!(code, 0, "{out}");
    assert!(out.contains("[renamed]"), "{out}");

    let (code, out) = repo.fence(&["update"]);
    assert_eq!(code, 0, "{out}");
    let (_, out) = repo.fence(&["check"]);
    assert!(out.contains("[ok]"), "{out}");
}

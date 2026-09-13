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

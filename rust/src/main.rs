//! Placeholder front end.
//!
//! Anchoring is ported and held to the shared conformance cases; the notes
//! store, the git plumbing and the hook are not. Until they are, the working
//! command is the Python one in `../python`.

fn main() {
    eprintln!("fence (rust): anchoring only so far -- no note store, git plumbing or hook yet.");
    eprintln!("The working command lives in ../python. Run `cargo test` to check this");
    eprintln!("implementation against ../conformance/cases.");
    std::process::exit(2);
}

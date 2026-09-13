# Building the Rust implementation

On Linux and macOS there is nothing to say:

```bash
cd rust
cargo test
```

CI does exactly that, on `ubuntu-latest`. The rest of this page is Windows, where
three things go wrong in ways whose error messages point somewhere else.

## A C compiler is required, not just a linker

The grammars are C. `tree-sitter-python` and `tree-sitter-typescript` compile
their parsers through the `cc` crate, so a C compiler has to exist before any of
this links — this is not the usual Windows story where Rust only needs a linker.
Without one the build reaches the grammar crates and stops:

```
error occurred in cc-rs: failed to find tool "gcc.exe": program not found
```

Two ways to supply one:

- **MSVC** — Visual Studio Build Tools with the C++ workload. Matches Rust's
  default `x86_64-pc-windows-msvc` toolchain, and wants 6–7 GB plus an elevated
  installer.
- **MinGW-w64** — `winget install BrechtSanders.WinLibs.POSIX.UCRT`, about
  1.5 GB, installs per-user without admin. Needs Rust's GNU toolchain alongside
  it: `rustup toolchain install stable-x86_64-pc-windows-gnu`.

Either is fine. The GNU route is what this project was last built with on
Windows, because it needs no administrator.

## Scope the toolchain to the directory, not the machine

If you took the GNU route, set the override on this directory rather than
changing the global default, so the rest of the machine keeps whatever Rust it
was using:

```bash
cd rust
rustup override set stable-x86_64-pc-windows-gnu
```

Overrides are keyed by absolute path, so moving or re-cloning the repository
loses this and the build reverts to the default toolchain — which, if MSVC is
not installed, fails at the link step rather than at anything that mentions
toolchains. Re-run the command after a move.

Both `cargo` and `gcc` must be on `PATH`. A fresh shell usually has `cargo`;
the WinLibs `gcc` lands under
`%LOCALAPPDATA%\Microsoft\WinGet\Packages\BrechtSanders.WinLibs.*\mingw64\bin`
and is not added for you.

## Keep `target/` out of anything that watches files

If the working copy lives somewhere a file watcher, sync client or scanner is
looking — a synced folder, or a directory an editor or tool indexes — cargo
loses a race against it while replacing archives, and the build fails with a
`.temp-archive` path and OS error 32:

```
error: failed to build archive at `...\libshlex-*.rlib`:
failed to remove temporary directory: The process cannot access the file
because it is being used by another process. (os error 32)
```

Nothing is wrong with the code or the toolchain; the file was held open for the
moment cargo wanted to move it. Which crate it names varies between runs, which
is the tell. Build somewhere else instead:

```bash
CARGO_TARGET_DIR=$TEMP/fence-target cargo test
```

This is also the cure for the long-path failures that hit the same setups, since
`target/` nests deeply enough to pass 260 characters on its own.

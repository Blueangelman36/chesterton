# Releasing

Everything here is prepared but nothing is published. The first upload claims the
name `chesterton` on PyPI permanently, so it is worth doing deliberately.

## Read this first

- **Publishing makes the source public.** The source distribution contains the code.
  If the repository is still private at that point, the package will also link to a
  repository nobody can open. Make the repository public in the same motion, or not
  at all.
- **A release cannot be withdrawn.** A version can be *yanked*, so new installs skip
  it, but it cannot be deleted and its number can never be reused.
- **`pip install chesterton` reads as "ready".** It handles Python source only. The
  package README says so; keep it saying so.
- The command is `fence`, but the package is `chesterton`, because `fence` on PyPI
  was taken in 2013 by an abandoned project.

## One-time setup

1. A PyPI account with 2FA enabled.
2. On PyPI, add a *pending publisher* for a project that does not exist yet:
   **Your projects → Publishing → Add a new pending publisher**

   | Field | Value |
   | --- | --- |
   | PyPI project name | `chesterton` |
   | Owner | `Blueangelman36` |
   | Repository | `chesterton` |
   | Workflow | `release.yml` |
   | Environment | `pypi` |

3. In the GitHub repository: **Settings → Environments → New environment → `pypi`**.
   Adding required reviewers here means a release waits for a human approval even
   after the tag is pushed.

No API token is created, stored, or rotated. The workflow authenticates with a
short-lived OIDC token that only that workflow can obtain.

## Releasing

```bash
# 1. Decide the version. It stays 0.x while the anchor format is still moving --
#    docs/FORMAT.md has changed twice already, and each change re-pins every note.
$EDITOR python/pyproject.toml

# 2. Check what would ship, from a clean checkout.
python -m build python
python -m twine check python/dist/*
python -m zipfile -l python/dist/chesterton-*.whl

# 3. Tag. The tag is what triggers the release workflow.
git tag -a v0.1.0 -m "First release"
git push origin v0.1.0
```

Then watch the run. The `publish` job waits on the `pypi` environment.

## Afterwards

```bash
pipx run --spec chesterton fence --help    # nothing installed permanently
uvx --from chesterton fence init           # the same, via uv
pip install chesterton
```

If something is wrong with a published version, `yank` it rather than trying to
delete it, and release a fixed version with a new number.

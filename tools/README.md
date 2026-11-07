# tools

The programs that develop, check and release HQ: what mise tasks, CI and the
operator's machine run. None of it is in the image (`Dockerfile.dockerignore`
admits nothing from here, and `image:verify:no-dev` fails an image that
carries any of it). So code that `hq` and `tools/` both use lives under `hq`,
and Ruff's TID251 (`banned-api` in `pyproject.toml`) refuses an import of
`tools` from outside it.

| Folder | What it holds |
| --- | --- |
| `dev/` | the development server, the local config examples to copy, and the vendoring of the API reference bundle |
| `fonts/` | the source font and the subset of it HQ serves (`mise run fix`) |
| `gate/` | the inner-loop gate, `check`, `test`'s runs by label and at a ref, and the list of shell files the shell gate checks |
| `lib/` | shell libraries the file tasks source: the heavy-gate lock, the tree export, the composed set, the source URL |
| `pipeline/` | CI and release programs: reports, composition, image checks, the release rehearsals |
| `structural/` | the structural bar and its baseline |
| `lint/` | the locked lint tools (axe-core, ESLint, Redocly, Stylelint), installed by `setup:lint-tools`; `lint/bcd/`, browser-compat-data, which `audit:bcd` installs for itself |
| `tests/` | the tests of the top-level modules |

The top level holds the credential minting (`credentials.py`) and the
permission lists it and its tests read, and `audit:image`'s reader of a
built image (`audit_image.py`).

Language: shell (POSIX sh) for a short pipeline of other programs; Python for
anything with logic, run through `uv run`, or plain stdlib at the runner's
floor Python where no toolchain is installed; Node only where the subject is
an npm package. Go lives in `controller/`.

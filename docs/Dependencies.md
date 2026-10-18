# Dependencies

Where every pin lives, and how to move one.

## Pins

| What | Pinned in | Held by |
| :--- | :--- | :--- |
| Tools: ruff, mypy, CodeQL, Scorecard and the rest | [`mise.toml`](../mise.toml), checksums in [`mise.lock`](../mise.lock) | `checks:toolchain`; CodeQL also `checks:codeql-bundle`, the bundle the pinned action runs |
| Python packages | `pyproject.toml`, resolved with hashes in `uv.lock` | `checks:lockfile`, `checks:audit` |
| uv | `pyproject.toml` (`tools` group), hashes in `uv.lock`; the images take uv's own image by digest | `checks:lockfile` |
| Go and its modules | `controller/go.mod` and `go.sum` | `controller:check` |
| Base images | `Dockerfile` and `composition/Dockerfile`, by digest | Dependabot |
| GitHub Actions | each workflow, by commit with its version beside it | `checks:pins` |
| Hosted runner image, Python matrix, coverage floor, Cordon commit, the Trivy release the image scans run | [`scripts/toolchain.env`](../scripts/toolchain.env) | `checks:runners`, `checks:badges` |
| cosign, on the host and in every workflow | `CosignVersion` in `controller/host/verifier.go`; workflows install it through `.github/actions/cosign` | `hq-host install` |
| The API reference bundle | `static/vendor/scalar/standalone.js`, version and SHA-256 in `UPSTREAM` | a test |

Dependabot (`.github/dependabot.yml`) proposes uv, Docker and Actions updates.

## Advisories

`mise run checks:audit` runs `pip-audit` against `uv.lock`, and the image
gates scan the built image with Trivy. When an advisory lands against a
transitive dependency, move only that package:

```sh
uv lock --upgrade-package <name>
```

An advisory that does not apply to the controller is recorded, with the
reason, in `controller/osv-scanner.toml`.

## Moving a pin

1. Change the pin where the table says, and regenerate its lock (`uv lock`,
   `mise lock`, `go mod tidy`).
2. `mise run check`, then `mise run ci`. A browser or Playwright change can
   move rendering: run `mise run browser`.
3. A change to the Python matrix changes the README badge; `checks:badges`
   fails until they agree.

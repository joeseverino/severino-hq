# Dependencies

Where every pin lives, and how to move one.

## Pins

| What | Pinned in | Held by |
| :--- | :--- | :--- |
| Tools: Go, uv, CodeQL, Scorecard and the rest | [`mise.toml`](../mise.toml), checksums in [`mise.lock`](../mise.lock). The images install Go and uv from the same lock, with mise copied from its image at `min_version`, by digest | `lint:toolchain`; CodeQL also `lint:codeql-bundle`, the bundle the pinned action runs |
| Python packages | `pyproject.toml`, resolved with hashes in `uv.lock` | `lint:lockfile`, `lint:audit` |
| npm tools: Redocly, the browser and stylesheet lints | `tools/lint/package.json`, exact, resolved with hashes in its `package-lock.json` | `npm ci`, which refuses a lock its `package.json` has moved past |
| Go and its modules | the release `controller/go.mod`'s `toolchain` line names, which `mise.toml` reads; modules in `go.sum` | `lint:toolchain`, `controller:check`; `MiseConfigTravelsTests`, every image that copies `mise.toml` copies `go.mod` beside it |
| The Python base image, every stage's start | `Dockerfile` and `composition/Dockerfile`, by digest | `image:build`; `lint:toolchain` refuses any other image |
| GitHub Actions | each workflow, by commit with its version beside it | `ActionPinTests` |
| Hosted runner image | each workflow's `runs-on`, one versioned label | `JobLimitTests` |
| Python matrix, coverage floor, Cordon commit | [`tools/pipeline/toolchain.env`](../tools/pipeline/toolchain.env) | `PythonVersionTests` |
| cosign, on the host and in every workflow | `CosignVersion` in `controller/host/verifier.go`; workflows install it through `.github/actions/cosign` | `hq-host install` |
| The API reference bundle | `static/vendor/scalar/standalone.js`, version and SHA-256 in `UPSTREAM` | a test |

## Updates

Renovate proposes every move above except cosign, whose release digest is
moved by hand with its version, and the Python matrix and coverage floor,
which are decisions. Its configuration is
[`.github/renovate.json`](../.github/renovate.json), each rule with its reason
beside it; [`.github/workflows/renovate.yml`](../.github/workflows/renovate.yml)
runs it every six hours under HQ's GitHub App, narrowed to this repository,
with the global configuration the host's runs take,
[`deploy/renovate/config.json`](../deploy/renovate/config.json),
and Renovate moves that workflow's own action and image too.

- **One weekly pull request**, `deps: update dependencies`, opened on Monday:
  every minor, patch and digest update across uv, mise, Actions, Go, npm,
  Docker, the runner image, Trivy, Cordon and the API reference bundle. A
  release is proposed once it is 7 days old.
- **Lock maintenance**, `deps: lock file maintenance`, the same day: it
  refreshes what only `uv.lock`, `mise.lock` and the npm locks name. Renovate
  never groups it with other updates.
- **Each major on its own pull request**, with its release notes, so a
  breaking change never holds the weekly one. A new Python or Go minor is
  treated as one: the matrix and the target versions move with it.
- **Security fixes at once**, outside the schedule, from GitHub's alerts and
  the OSV database, in one pull request. A Go patch release is a security
  release that OSV sees through `go.mod`'s toolchain line, so it opens
  at once too, as `go patch`.
- At most three of these are open at a time, security fixes aside. Nothing
  merges itself: a merge to `main` deploys after an approval, and that
  approval is the review.

The Dependency Dashboard issue lists what is pending, held for age or
blocked. A Renovate pull request is worked like any other: push the code a
major needs onto its branch. To vendor a new API reference bundle by hand,
change `version:` in `static/vendor/scalar/UPSTREAM` and run
`node tools/dev/vendor-scalar.mjs`.

## Advisories

`mise run lint:audit` runs `uv audit` against `uv.lock`, every group
included, and an
extension's admission audits its lock through the same
`tools/pipeline/audit.sh`. The image gates scan the built image with
Trivy. When an advisory lands against a transitive dependency, move only
that package:

```sh
uv lock --upgrade-package <name>
```

An advisory that does not apply to the controller is recorded, with the
reason, in `controller/osv-scanner.toml`.

## Moving a pin

1. Change the pin where the table says, and regenerate its lock (`uv lock`,
   `mise lock`, `go mod tidy`).
2. `mise run check`. A browser or Playwright change can move rendering: it
   runs `mise run browser` among its gates, the one to repeat.
3. A change to the Python matrix changes the README badge; `PythonVersionTests`
   fails until they agree.

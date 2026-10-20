# Dependencies

Where every pin lives, and how to move one.

## Pins

| What | Pinned in | Held by |
| :--- | :--- | :--- |
| Tools: uv, CodeQL, Scorecard and the rest | [`mise.toml`](../mise.toml), checksums in [`mise.lock`](../mise.lock) | `checks:toolchain`; CodeQL also `checks:codeql-bundle`, the bundle the pinned action runs |
| Python packages | `pyproject.toml`, resolved with hashes in `uv.lock` | `checks:lockfile`, `checks:audit` |
| uv in the images | `Dockerfile` and `composition/Dockerfile`, uv's own image by digest at the version `mise.toml` pins | `checks:toolchain` |
| Go and its modules | `controller/go.mod` and `go.sum` | `controller:check` |
| Base images | `Dockerfile` and `composition/Dockerfile`, by digest | `image:build` |
| GitHub Actions | each workflow, by commit with its version beside it | `checks:pins` |
| Hosted runner image, Python matrix, coverage floor, Cordon commit, the Trivy release the image scans run | [`tools/pipeline/toolchain.env`](../tools/pipeline/toolchain.env) | `checks:runners`, `checks:badges` |
| cosign, on the host and in every workflow | `CosignVersion` in `controller/host/verifier.go`; workflows install it through `.github/actions/cosign` | `hq-host install` |
| The API reference bundle | `static/vendor/scalar/standalone.js`, version and SHA-256 in `UPSTREAM` | a test |

## Updates

Renovate proposes every move above except cosign, whose release digest is
moved by hand with its version, and the Python matrix and coverage floor,
which are decisions. Its configuration is
[`.github/renovate.json`](../.github/renovate.json), each rule with its reason
beside it; [`.github/workflows/renovate.yml`](../.github/workflows/renovate.yml)
runs it every six hours under HQ's GitHub App, narrowed to this repository,
and Renovate moves that workflow's own action and image too.

- **One weekly pull request**, `deps: update dependencies`, opened on Monday:
  every minor, patch and digest update across uv, mise, Actions, Go, npm,
  Docker, the runner image, Trivy, Cordon and the API reference bundle. A
  release is proposed once it is 14 days old.
- **Lock maintenance**, `deps: lock file maintenance`, the same day: it
  refreshes what only `uv.lock`, `mise.lock` and the npm lock name. Renovate
  never groups it with other updates.
- **Each major on its own pull request**, with its release notes, so a
  breaking change never holds the weekly one. A new Python or Go minor is
  treated as one: the matrix and the target versions move with it.
- **Security fixes at once**, outside the schedule, from GitHub's alerts and
  the OSV database, in one pull request.
- At most three of these are open at a time, security fixes aside. Nothing
  merges itself: a merge to `main` deploys after an approval, and that
  approval is the review.

The Dependency Dashboard issue lists what is pending, held for age or
blocked. A Renovate pull request is worked like any other: push the code a
major needs onto its branch. To vendor a new API reference bundle by hand,
change `version:` in `static/vendor/scalar/UPSTREAM` and run
`node tools/dev/vendor-scalar.mjs`.

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

# Blueprint Setup

Every *instance-specific* value: what someone running their own HQ from this
repository must change. Three kinds exist:

1. **Code constants.** Compiled into a program or read by a test. Change the
   one declaration; the consumers follow.
2. **Outside the repository.** GitHub settings, the vault, and files on the
   host. Nothing here names them.
3. **Residue.** Static text that names this instance and cannot import a
   constant. Change it by hand.

## 1. Code constants

| Value | Set in | Used by |
| :--- | :--- | :--- |
| The repository `hq-host deploy` accepts: its composition `ghcr.io/<owner>/<repo>/composition`, signed by its Compose workflow on `main` | nowhere by hand: CI builds `hq-host` with `HQ_SOURCE` from `github.repository` (`BuildSource` in [`controller/host/deploy.go`](../controller/host/deploy.go)) | `hq-host deploy`, which refuses any other image; a build without a source deploys nothing |
| The Go module path, `github.com/<owner>/<repo>/controller` | [`controller/go.mod`](../controller/go.mod) and every import under `controller/` | the Go build |
| The Cordon repository and commit | `repository:` in `.github/actions/admit-plugin/action.yml` and `.github/workflows/compose.yml`; `CORDON_COMMIT` in [`tools/pipeline/toolchain.env`](../tools/pipeline/toolchain.env) | extension admission and Compose |
| The host repository extensions are checked against | `HOST_REPOSITORY` in `.github/workflows/plugin-checks.yml` | an extension's checks |
| The failure-log recipient (an age public key) | `FAILURE_LOG_RECIPIENT` in [`tools/pipeline/toolchain.env`](../tools/pipeline/toolchain.env) | `tools/pipeline/seal-failure-logs.sh` ([Deployment](Deployment.md#when-a-run-fails)) |
| The runner label, `severino-hq` | `runs-on` of the Deploy job in `.github/workflows/deploy.yml` | Deploy |
| The required check, **Ready** | the `ready` job's name in `.github/workflows/ci.yml` | the ruleset's required check |
| The extension check, **Severino HQ · Production** | the admission action and the controller (`controller/providers/github_delivery.go`) | an extension's commit |
| The site name | `SEVERINO_SITE_NAME` (default in `hq/config/settings.py`) | page titles and the header |

### Host paths

These are constants of the host programs and units. A host provides them; a
fork changes them in every place listed.

| Path | Declared in |
| :--- | :--- |
| `/usr/local/lib/severino-hq`, `/var/lib/severino-hq`, `/run/severino-hq-secrets`, `/run/severino-hq`, `/var/log/severino-hq` | [`controller/host/layout.go`](../controller/host/layout.go) and, for the two under `/run`, [`controller/internal/hostcore/hostcore.go`](../controller/internal/hostcore/hostcore.go); `controller/cmd/hq-host/main.go`, `deploy/systemd/` |
| `/opt/apps/severino-hq/secrets` (renderer lock, ACME state) | `ACMEDir` in `layout.go`; `SEVERINO_HQ_SECRET_DIR` default in `controller/cmd/hq-secrets/main.go`; `severino-hq-secrets.service`, `severino-hq-controller.service` |
| `/srv/severino-hq/backups` (backup archives) | `BackupDir` in `controller/host/backup.go`, `severino-hq-backup.service`, `deploy/tmpfiles/severino-hq.conf` |
| The web account, UID/GID `10001` | `WebUID` in `controller/internal/hostcore/hostcore.go`, the `Dockerfile`, `deploy/tmpfiles/severino-hq.conf` |

## 2. Outside the repository

### GitHub

| Value | Where | Read by |
| :--- | :--- | :--- |
| `HQ_APP_KEY`, `HQ_APP_CLIENT_ID` | repository secrets, set by `mise run wire-github-app` | CI's Release and Compose: reading admissions and starting the next workflow |
| `COMPOSITION_EXTENSIONS` | repository secret: JSON in the shape of [`composition/extensions.json`](../composition/extensions.json) | Compose: which extensions to compose, and whose signatures to trust |
| The `release` environment, `main` only, with the deploy review App as its deployment protection rule, and the `HQ_REVIEW_APP_ID` repository secret | set by `mise run wire-github-app --review` | Deploy's approval, which Deploy refuses to reach until both are in place |
| The `admission` environment on each extension, `main` only, holding `HQ_APP_KEY` and `HQ_APP_CLIENT_ID` | each extension's settings, set by `mise run wire-github-app` | an admission starting Compose |
| HQ's GitHub App: the permissions in [`deploy/github-apps.json`](../deploy/github-apps.json), installed on the host and every extension, its webhook the inbound-events Worker's github route | the account's app settings; the webhook set by `mise run wire-github-app --webhook-secret` ([Install](Install.md#12-hear-providers-events-and-approve-deploys-in-hq)) | everything above, and the controller |
| The deploy review App: the permissions in [`deploy/github-apps.json`](../deploy/github-apps.json), installed on the host alone, its webhook the Worker's github route | the account's app settings, wired by `mise run wire-github-app --review` ([Install](Install.md#12-hear-providers-events-and-approve-deploys-in-hq)) | the controller, answering a deploy waiting on `release` |
| The `inbound-worker` environment, the operator its required reviewer, holding `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` and `HQ_INBOUND_HOSTNAME` | the repository's settings ([Install](Install.md#12-hear-providers-events-and-approve-deploys-in-hq)) | Deploy Worker |
| A `main` ruleset requiring **Ready** from GitHub Actions, with code scanning holding a merge on a CodeQL alert | repository rules | merges |
| A self-hosted runner with the `severino-hq` label | the host ([Install](Install.md#6-install-the-runner-and-write-hostjson)) | Deploy |

### `host.json`

`/etc/severino-hq/host.json` on the host, in the shape of
[`deploy/host.example.json`](../deploy/host.example.json), holds the vault,
Connect, the runner and any grants.
[Install](Install.md#6-install-the-runner-and-write-hostjson) says what each
field is set to, and `hq-host bootstrap` renders the host's drop-ins from it
([Secrets](Secrets.md#bootstrap-and-ownership)).

### The vault

| Item | Holds |
| :--- | :--- |
| The application-environment item (`env_item`) | the variables in [Install](Install.md#7-create-the-application-environment); the renderer refuses an item without a required one and names it |
| One item per connection, none to start | `connection_ref`, `projection`, `env_prefix`, and `manages` where HQ may act ([Secrets](Secrets.md#connections)) |
| SSH Key items | the controller's deployment identities and the GitHub App's signing key |

The domains HQ serves and manages are not configured anywhere: they are what
the connections can see.

### Local and per-host

| Value | Where |
| :--- | :--- |
| `/etc/severino-hq/backup.env`: `SEVERINO_BACKUP_AGE_RECIPIENTS` | the host ([Backup](Backup.md)) |
| `SEVERINO_HQ_DEPLOY_HOST`, an SSH destination | the shell running `mise run check --release` |
| `HQ_LOCAL_PYTHONPATH`, `HQ_LOCAL_PLUGINS` | `mise.local.toml` ([Development](Development.md#the-extension-set)) |
| `SEVERINO_FAILURE_LOG_IDENTITY` | the operator's shell, for `mise run failure-logs` |
| `GITHUB_WEBHOOK_SECRET`, `SOCKET_TOKEN` | a 1Password item of the operator's, for `mise run worker:deploy --with-secrets` and `mise run wire-github-app` ([Install](Install.md#12-hear-providers-events-and-approve-deploys-in-hq)) |
| Internal CA roots | `/usr/local/share/ca-certificates` on the host |

## 3. Residue

- [`README.md`](../README.md): the badge URLs (`github.com/<owner>/<repo>`).
- [`.github/SECURITY.md`](../.github/SECURITY.md): the reporting address.
- `Documentation=` in `deploy/systemd/*`: the repository URL.
- The outbound `User-Agent` strings in
  `hq/domains/content/content_sync.py` and `controller/providers/glance.go`.
- [`tools/pipeline/admission_policy.py`](../tools/pipeline/admission_policy.py):
  `host`, then `mise run fix`, which writes `policy/plugin-admission-v1.json`.
- [`LICENSE`](../LICENSE): the copyright holder.

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
| `/usr/local/lib/severino-hq`, `/var/lib/severino-hq`, `/run/severino-hq-secrets`, `/run/severino-hq`, `/var/log/severino-hq` | [`controller/host/layout.go`](../controller/host/layout.go), `controller/cmd/hq-host/main.go`, `deploy/systemd/` |
| `/opt/apps/severino-hq/secrets` (renderer lock, ACME state) | `ACMEDir` in `layout.go`; `SEVERINO_HQ_SECRET_DIR` default in `controller/cmd/hq-secrets/main.go`; `severino-hq-secrets.service`, `severino-hq-controller.service` |
| `/opt/apps` (the stacks container upgrades may touch) | `StacksRoot` in `controller/host/upgrade/upgrade.go`, `STACKS_ROOT` in `hq/platform/application/upgrades.py` |
| `/srv/severino-hq/backups` (backup archives) | `BackupDir` in `controller/host/backup.go`, `severino-hq-backup.service`, `deploy/tmpfiles/severino-hq.conf` |
| The web account, UID/GID `10001` | `WebUID` in `layout.go`, the `Dockerfile`, `deploy/tmpfiles/severino-hq.conf` |

## 2. Outside the repository

### GitHub

| Value | Where | Read by |
| :--- | :--- | :--- |
| `HQ_APP_KEY`, `HQ_APP_CLIENT_ID` | repository secrets, set by `mise run wire-github-app` | CI's Release and Compose: reading admissions and starting the next workflow |
| `COMPOSITION_EXTENSIONS` | repository secret: JSON in the shape of [`composition/extensions.json`](../composition/extensions.json) | Compose: which extensions to compose, and whose signatures to trust |
| The `production` environment, with required reviewers | repository environments | Deploy's approval |
| The `admission` environment on each extension, `main` only, holding `HQ_APP_KEY` and `HQ_APP_CLIENT_ID` | each extension's settings, set by `mise run wire-github-app` | an admission starting Compose |
| HQ's GitHub App: no webhook, the permissions in [`deploy/github-apps.json`](../deploy/github-apps.json), installed on the host and every extension | the account's app settings | everything above, and the controller |
| A `main` ruleset requiring **Ready** from GitHub Actions, with code scanning holding a merge on a CodeQL alert | repository rules | merges |
| A self-hosted runner with the `severino-hq` label | the host ([Install](Install.md#6-install-the-runner-and-write-hostjson)) | Deploy |

### `host.json`

`/etc/severino-hq/host.json` on the host, in the shape of
[`deploy/host.example.json`](../deploy/host.example.json). `hq-host
bootstrap` renders it ([Secrets](Secrets.md#bootstrap-and-ownership)).

| Field | Is |
| :--- | :--- |
| `vault` | the 1Password vault every credential is read from |
| `env_item` | the item holding the application environment |
| `connect.unit` | the systemd service running Connect; the renderer requires it |
| `connect.url` | Connect's address, `http://127.0.0.1:PORT`, with a port below 1024, such as `880` |
| `connect.credential` | the encrypted credential in `/etc/credstore.encrypted/` holding the reader token |
| `runner.unit` | the Actions runner's service unit |
| `runner.user` | the account the runner runs as; not root |

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
| Internal CA roots | `/usr/local/share/ca-certificates` on the host |

## 3. Residue

- [`README.md`](../README.md): the badge URLs (`github.com/<owner>/<repo>`).
- [`.github/SECURITY.md`](../.github/SECURITY.md): the reporting address.
- `Documentation=` in `deploy/systemd/*`: the repository URL.
- The outbound `User-Agent` strings in
  `hq/domains/content/content_sync.py` and `controller/providers/glance.go`.
- [`policy/plugin-admission-v1.json`](../policy/plugin-admission-v1.json):
  `host`.
- [`LICENSE`](../LICENSE): the copyright holder.

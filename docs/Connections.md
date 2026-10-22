# Connections

What to create for each service HQ connects to, in the order to connect them,
and the 1Password item the controller reads it from. Written by `manage.py
connections_reference` from what HQ expects of each service; the Connections
page shows the same, for each one not yet connected. A test fails when this
file falls behind.

How a connection item works in general is in [Secrets](Secrets.md#connections).

## Cloudflare DNS

Your domains and their public DNS records.

A custom API token in the Cloudflare dashboard, under My Profile, API Tokens, limited to your zones. [Where to make it](https://dash.cloudflare.com/profile/api-tokens)

Permissions, and no others:

- `DNS Write (zone)`
- `Zone Read (zone)`

Store it as an API Credential item:

| Field | Value | What it is |
| --- | --- | --- |
| `connection_ref` | `cloudflare-dns` | Any name; how HQ refers to this connection. |
| `projection` | `api_token` | The shape the controller reads the item as. |
| `env_prefix` | `CLOUDFLARE_DNS` | Says which service it is for. |
| `credential` | the secret | The item's own credential field. |
| `website` | yours | A field labeled website. |
| `manages` | `1` | Optional. Lets HQ make changes through it, not only read. |

## Portainer

Every container on your machines, with its image, stack, volumes and networks.

An access token in Portainer, under My account, Access tokens. [Where to make it](https://docs.portainer.io/api/access)

This is the whole account: Portainer offers nothing narrower, so make it for HQ alone.

Store it as an API Credential item:

| Field | Value | What it is |
| --- | --- | --- |
| `connection_ref` | `portainer` | Any name; how HQ refers to this connection. |
| `projection` | `api_token` | The shape the controller reads the item as. |
| `env_prefix` | `PORTAINER` | Says which service it is for. |
| `credential` | the secret | The item's own credential field. |
| `website` | yours | A field labeled website. |
| `manages` | `1` | Optional. Lets HQ make changes through it, not only read. |

## Tailscale

Your tailnet's devices, who may reach what, and its DNS.

An OAuth client in the Tailscale admin console, under Settings, Trust credentials. [Where to make it](https://tailscale.com/kb/1215/oauth-clients)

Permissions, and no others:

- `devices:core:read`
- `devices:routes:read`
- `dns:read`
- `feature_settings:read`
- `networking_settings:read`
- `policy_file:read`
- `services:read`
- `users:read`

Or have `mise run mint:tailscale` issue one with exactly these.

Store it as an API Credential item:

| Field | Value | What it is |
| --- | --- | --- |
| `connection_ref` | `tailscale` | Any name; how HQ refers to this connection. |
| `projection` | `oauth_client` | The shape the controller reads the item as. |
| `env_prefix` | `TAILSCALE` | Says which service it is for. |
| `username` | yours | The item's own username field. |
| `credential` | the secret | The item's own credential field. |
| `manages` | `1` | Optional. Lets HQ make changes through it, not only read. |

## Nginx Proxy Manager

Your proxy hosts, their certificates, and whom each one lets in.

A user in Nginx Proxy Manager, under Users, allowed to manage proxy hosts and certificates. [Where to make it](https://nginxproxymanager.com/guide/)

This is the whole account: Nginx Proxy Manager offers nothing narrower, so make it for HQ alone.

Store it as a Login item:

| Field | Value | What it is |
| --- | --- | --- |
| `connection_ref` | `npm` | Any name; how HQ refers to this connection. |
| `projection` | `login` | The shape the controller reads the item as. |
| `env_prefix` | `NPM` | Says which service it is for. |
| `password` | the secret | The item's own password field. |
| `website` | yours | The item's website: the service's address. |
| `username` | yours | The item's own username field. |
| `manages` | `1` | Optional. Lets HQ make changes through it, not only read. |

## Cloudflare API

Your site's visits and contact-form messages, its tunnels, Access apps and Pages projects.

A custom API token in the Cloudflare dashboard, under My Profile, API Tokens. [Where to make it](https://dash.cloudflare.com/profile/api-tokens)

Permissions, and no others:

- `Access: Apps Read (account)`
- `Access: Policies Read (account)`
- `Access: Service Tokens Read (account)`
- `Account Analytics Read (account)`
- `Account Settings Read (account)`
- `Cloudflare Pages Read (account)`
- `Cloudflare Tunnel Read (account)`
- `D1 Read (account)`
- `D1 Write (account)`
- `Page Rules Read (zone)`
- `Registrar: Domains Read (account)`
- `SSL and Certificates Read (zone)`
- `Single Redirect Read (zone)`
- `Zone Read (zone)`
- `Zone Settings Read (zone)`

Or have `mise run mint:cloudflare` issue one with exactly these.

Store it as an API Credential item:

| Field | Value | What it is |
| --- | --- | --- |
| `connection_ref` | `cloudflare-api` | Any name; how HQ refers to this connection. |
| `projection` | `api_token` | The shape the controller reads the item as. |
| `env_prefix` | `CLOUDFLARE_API` | Says which service it is for. |
| `credential` | the secret | The item's own credential field. |
| `website` | yours | A field labeled website. |
| `manages` | `1` | Optional. Lets HQ make changes through it, not only read. |

## AdGuard Home

Your internal DNS records, and which devices look up what.

A login for AdGuard Home's web interface: the one made on first run, or another under users in AdGuardHome.yaml. [Where to make it](https://github.com/AdguardTeam/AdGuardHome/wiki/Configuration)

This is the whole account: AdGuard Home offers nothing narrower, so make it for HQ alone.

Store it as a Login item:

| Field | Value | What it is |
| --- | --- | --- |
| `connection_ref` | `adguard` | Any name; how HQ refers to this connection. |
| `projection` | `login` | The shape the controller reads the item as. |
| `env_prefix` | `ADGUARD` | Says which service it is for. |
| `password` | the secret | The item's own password field. |
| `website` | yours | The item's website: the service's address. |
| `username` | yours | The item's own username field. |
| `manages` | `1` | Optional. Lets HQ make changes through it, not only read. |

## GitHub App

Repo checks on your repositories, and HQ's deploys waiting for your approval.

A GitHub App on your account; mise run wire-github-app records its ID and stores its key. [Where to make it](https://github.com/settings/apps/new)

Permissions, and no others:

- `Actions variables: read`
- `Dependabot alerts: read`
- `actions: read`
- `administration: read`
- `checks: read`
- `code scanning alerts: read`
- `commit statuses: write`
- `contents: read`
- `contents: write`
- `deployments: read`
- `issues: write`
- `leaked credential alerts: read`
- `metadata: read`
- `packages: read`
- `pull requests: read`
- `pull requests: write`
- `workflows: write`

Store it as an item of any type:

| Field | Value | What it is |
| --- | --- | --- |
| `connection_ref` | `github-app` | Any name; how HQ refers to this connection. |
| `projection` | `github_app` | The shape the controller reads the item as. |
| `env_prefix` | `GITHUB_APP` | Says which service it is for. |
| `app_id` | yours | A field labeled app_id. |
| `signing_key` | yours | A field labeled signing_key. |
| `manages` | `1` | Optional. Lets HQ make changes through it, not only read. |

## SSH

Caddy routes and certificates on your machines, and which of their ports answer.

An SSH key made in 1Password, its public half in the machine's authorized_keys. [Where to make it](https://developer.1password.com/docs/ssh/manage-keys/)

This is the whole account: SSH offers nothing narrower, so make it for HQ alone.

Store it as an item of any type:

| Field | Value | What it is |
| --- | --- | --- |
| `connection_ref` | `ssh` | Any name; how HQ refers to this connection. |
| `projection` | `ssh_transport` | The shape the controller reads the item as. |
| `env_prefix` | `SSH` | Says which service it is for. |
| `host` | yours | A field labeled host. |
| `host_key` | yours | A field labeled host_key. |
| `identity` | yours | A field labeled identity. |
| `port` | yours | A field labeled port. |
| `role` | yours | A field labeled role. Optional. |
| `user` | yours | A field labeled user. |
| `manages` | `1` | Optional. Lets HQ make changes through it, not only read. |

## 1Password

Renewed certificates published to a vault, for machines that fetch their own.

A 1Password service account that can write only to the vault certificates are published to. HQ reads its own secrets through Connect, not this. [Where to make it](https://developer.1password.com/docs/service-accounts/get-started/)

Store it as an API Credential item:

| Field | Value | What it is |
| --- | --- | --- |
| `connection_ref` | `onepassword` | Any name; how HQ refers to this connection. |
| `projection` | `service_account` | The shape the controller reads the item as. |
| `env_prefix` | `ONEPASSWORD` | Says which service it is for. |
| `credential` | the secret | The item's own credential field. |
| `manages` | `1` | Optional. Lets HQ make changes through it, not only read. |

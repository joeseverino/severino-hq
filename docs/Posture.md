# Posture

Every HQ enforces the checks in the first table below. The second table is
HQ's standard: shown on every HQ, on the connection panel and the Connections
page, each with what it stops. Written by `manage.py posture_reference` from
each check's declaration, a `standards.Check`; a test fails when this file
falls behind.

**Advanced HQ posture** lists each unmet check of the standard in Needs you,
with the step that meets it. The panel then reads **Advanced HQ posture
enabled**, with a lock.

## Turning it on

Set `SEVERINO_POSTURE=advanced` in HQ's settings
([Deployment](Deployment.md#configuration)). HQ will not start on any other
value.

## Every HQ

Every request is measured against these, at HQ's own step of its path
(`REQUEST_STANDARD`, `hq/platform/application/request_standard.py`):

| Check | Stops | Enforced by |
| --- | --- | --- |
| Other networks are refused | HQ answering the internet | HQ's trusted-network setting |
| Session cookie is locked to this site | a cookie stolen or set by another host | HQ's cookie settings |
| HTTPS only | a downgrade to plain HTTP | HTTPS redirect and HSTS |
| Scripts only from HQ | injected script | Content Security Policy |

The deploy and the host enforce the rest, which no request can show:

| Guarantee | Stops | Enforced by |
| --- | --- | --- |
| Sign-in is required | an anonymous visitor | HQ's access rules |
| HQ trusts only the proxies it lists | a forged client address | the trusted-proxy list |
| HQ never holds a credential | a compromised web process leaking keys | the renderer and controller split |
| Secrets are read from a loopback Connect server | a stolen Connect token used from anywhere | `hq-host` refuses any other address in `host.json` |
| Images are signed and pinned by digest | running a tampered build | the deploy's signature check |
| No extension is dropped by a release | a release silently removing a domain | the deploy refuses an undeclared drop |

## HQ's standard

Declared in `STANDARD` (`hq/platform/application/posture.py`). The panel, the
findings, the Connections page and its API read it.

| Check | Stops | Enforced by |
| --- | --- | --- |
| The proxy lets in tailnet addresses only | the proxy exposing HQ beyond the tailnet | The proxy's access list |
| The firewall requires the tailnet interface | a forged tailnet address arriving another way | The host firewall's rule for HQ's port |
| The tailnet policy has no allow-all rule | any device on the tailnet opening HQ | The tailnet policy |
| Tailnet lock is on | a device added to your tailnet without your key | Tailnet lock, read by the host's own daemon |
| Sign-in is single sign-on only | a guessed or reused password | HQ's sign-in settings |
| Tokens hold the permissions HQ uses, and no more | a stolen token reaching the whole account | Each connection's permission check |
| Deploys wait for your approval | a merge reaching production before you see it | The production environment's reviewers, read by the GitHub App |

A check with no reading yet is shown as not read, never as met. A login, such
as Nginx Proxy Manager's, has no scoped form, so it meets the token check
with its full account access named.

The proxy and firewall checks are drawn on the connection panel too, from
the same reading. Two checks are shown only on the connection panel, because
they describe one visit: whether the device's owner is the person signed in,
and whether HQ's own machine measured the link.

Single sign-on belongs to the standard, since not every homelab runs an
identity provider.

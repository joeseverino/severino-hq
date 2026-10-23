# Posture

Every HQ enforces the checks in the first table below. The second table is
HQ's standard: shown on every HQ, on the connection panel and the Connections
page, each with what it stops.

**Advanced HQ Posture** lists each unmet check of the standard in Needs you,
with the step that meets it. The panel then reads **Advanced HQ Posture
enabled**, with a lock.

## Turning it on

Add `SEVERINO_POSTURE=advanced` to the env item in 1Password. HQ refuses to
start on any other value.

## Every HQ

| Check | Stops | Enforced by |
| --- | --- | --- |
| Other networks are refused | HQ answering the internet | the trusted-network setting |
| Sign-in is required | an anonymous visitor | HQ's access rules |
| Scripts only from HQ | injected script | the CSP HQ sends |
| Session cookie is locked to this site | a cookie stolen or set by another host | the cookie settings |
| HTTPS only, with HSTS | a downgrade to plain HTTP | the redirect and HSTS settings |
| HQ trusts only the proxies it lists | a forged client address | the trusted-proxy list |
| HQ never holds a credential | a compromised web process leaking keys | the renderer and controller split |
| Secrets are read from a loopback Connect server | a stolen Connect token used from anywhere | `hq-host bootstrap` refuses any other address |
| Images are signed and pinned by digest | running a tampered build | the deploy's signature check |
| No extension is dropped by a release | a release silently removing a domain | the deploy refuses an undeclared drop |

## HQ's standard

| Check | Stops | Read from |
| --- | --- | --- |
| The proxy lets in tailnet addresses only | the proxy exposing HQ beyond the tailnet | the proxy's access list for HQ's name |
| The firewall requires the tailnet interface | a forged tailnet address arriving another way | the host firewall reading |
| The tailnet policy has no allow-all rule | any device on the tailnet opening HQ | the tailnet policy |
| Tailnet lock is on | a device added to your tailnet without your key | the host's tailnet lock reading, from its own daemon |
| Sign-in is single sign-on only | a guessed or reused password | the sign-in backends |
| Each token is scoped where its service allows it | a stolen token reaching the whole account | each connection's credential type |
| Deploys wait for your approval | a merge reaching production before you see it | the production environment's reviewers, read by the GitHub App |

A check with no reading yet is shown as not read, never as met. A login, such
as Nginx Proxy Manager's, has no scoped form, so it meets the token check.

Each check is declared once, in `STANDARD` (`hq/platform/application/posture.py`).
The panel, the findings and the Connections API read it.

Two checks are shown only on the connection panel, because they describe
one visit: whether the device's owner is the person signed in, and whether
HQ's own machine measured the link.

Single sign-on belongs to the standard, since not every homelab runs an
identity provider.

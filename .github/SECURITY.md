# Security policy

Severino HQ is a single-user internal application. It is intended to run
**only over a private Tailscale network** behind authentication. The
project's security model is documented in [`docs/SECURITY.md`](../docs/SECURITY.md).

## Reporting a vulnerability

If you find a security issue, please **do not** open a public GitHub issue.

- Email: `security@<this-domain>` (replace with the operator's address).
- Or, if you have GitHub private-vulnerability-reporting access on this repo,
  use **Security → Advisories → Report a vulnerability**.

You'll get an acknowledgement within 7 days. Please include enough detail to
reproduce and, where applicable, a suggested mitigation.

## What's in scope

- Auth bypass on any Severino HQ route.
- Receipt file disclosure to unauthenticated parties.
- CSRF / XSS / injection in the admin or app UI.
- Exposure of secret-adjacent documentation records via exports.
- Container escape / privilege escalation in the shipped Docker image.

## What's out of scope

- Reports about missing rate limiting (the app is meant to be reachable only
  over Tailscale).
- Reports about DEBUG mode behaviour (production requires `DEBUG=0` at
  startup).
- Anything that requires already-authenticated admin access.

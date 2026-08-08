# Security policy

Severino HQ is a single-user internal application. It is intended to run
**only over a private Tailscale network** behind authentication. The
project's security model is documented in [`docs/SECURITY.md`](../docs/SECURITY.md).

## Reporting a vulnerability

If you find a security issue, please **do not** open a public GitHub issue.

- Email: `security@jseverino.com`.
- Or, if you have GitHub private-vulnerability-reporting access on this repo,
  use **Security → Advisories → Report a vulnerability**.

You'll get an acknowledgement within 7 days. Please include enough detail to
reproduce and, where applicable, a suggested mitigation.

## What's in scope

- Auth bypass on any Severino HQ route.
- Receipt file disclosure to unauthenticated parties.
- CSRF / XSS / injection in the admin or app UI.
- Exposure of restricted documentation records via exports.
- Container escape / privilege escalation in the shipped Docker image.
- Privilege escalation or destructive-action bypass by an authenticated user.
- Authentication throttling or session weaknesses with a reproducible impact.

## What's out of scope

- Volumetric public-internet denial of service; HQ is reachable only through
  the private tailnet.
- Reports about DEBUG mode behaviour (production requires `DEBUG=0` at
  startup).
- Reports that assume root access to the host or control of the private
  tailnet without demonstrating an additional boundary failure.

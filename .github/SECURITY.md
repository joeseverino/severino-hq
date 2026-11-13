# Security policy

Severino HQ is a self-hosted operations application. Each person who signs
in holds a role of their own. It runs **only over a private
Tailscale network**, behind sign-in. The security model is in
[`docs/Security.md`](../docs/Security.md).

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
- CSRF, XSS or injection in the web UI, the API or MCP.
- Exposure of restricted documentation records via exports.
- Container escape or privilege escalation in the shipped Docker image.
- Privilege escalation or destructive-action bypass by a signed-in person,
  or by an agent beyond the role of the person it acts for.
- Authentication throttling or session weaknesses with a reproducible impact.

## What's out of scope

- Volumetric public-internet denial of service; HQ is reachable only through
  the private tailnet.
- Reports about DEBUG mode behavior (production requires `DEBUG=0` at
  startup).
- Reports that assume root access to the host or control of the private
  tailnet without demonstrating an additional boundary failure.

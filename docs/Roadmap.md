# Roadmap

What HQ may take on next, and what it will not.

## Next

- **Container upgrades HQ runs on other machines.** HQ plans an upgrade on
  any machine it reads, and runs it only on its own host. On another machine
  you run the plan's commands by hand
  ([Container upgrades](Deployment.md#container-upgrades)).
- **PostgreSQL, when evidence asks for it:** more than one writer, WAL lock
  failures under normal use, maintenance pauses that matter, or a query that
  needs it. The ORM and migrations are database-agnostic; SQLite-specific
  pragmas and full-text paths sit behind replaceable boundaries.
- **Bulk import** of expenses from CSV.
- **Saved filters** on list pages.

## Not planned

- Invoices, payments, bank logins, payroll, clients or leads.
- A customer portal, public registration, or more than one tenant.
- Anything reachable from the public internet, or public webhooks.
- Secrets, runbook bodies or receipt files in AI-readable exports.
- Replacing the notes vault. HQ indexes pointers to documentation; the vault
  stays the source of truth.

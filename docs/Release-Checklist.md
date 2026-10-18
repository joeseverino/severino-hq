# Release Checklist

From a branch to a change live in production. `main` is protected; it changes
only by a merged pull request.

## 1. Before pushing

- [ ] `mise run check` passes, with `mise.local.toml` naming the extension set
      so the composed pass runs.
- [ ] A change to templates or CSS passes `mise run browser` and has been
      walked in a real browser through the workflow it serves.
- [ ] A changed route, capability, SDK export or connection shape has its
      derived files regenerated: `mise run regen`.
- [ ] A new constraint on a table that holds rows has passed
      `manage.py constraint_preflight --path <database>` against production
      data.
- [ ] `mise run ci` passes.
- [ ] The diff names no private identifier, secret or extension.

## 2. The pull request

- [ ] One pull request per piece of work, with the repository's open
      dependency updates folded in.
- [ ] **Severino HQ · Review** says *Ready to merge*. *Held* names the gate
      and the fix.
- [ ] `mise run preflight` (or `mise run preflight --remote` after the push)
      exits 0: every gate, the composed suite with the extensions required,
      and the deploy host read-only.

## 3. Merge and deploy

- [ ] After the merge, CI promotes the proven image and starts Compose.
- [ ] **Severino HQ · Production** reaches *Waiting for approval*. Approve the
      Deploy run in the `production` environment. A newer release waiting
      supersedes an older one; reject a stale one rather than approving it.
- [ ] A release that migrates is rehearsed first: `mise run shadow-run IMAGE`
      with the image it waits to deploy runs its migrations, bridge, jobs and
      controller plan on a copy of production's database on the deploy host,
      touching nothing live. Approve only on `result=pass`.
- [ ] The Deploy job is green and the check says *Live in production*. CI
      green is not live: only the Deploy job deploys.

## 4. If it is not live

- [ ] The check names the failed step and its fix
      ([Deployment](Deployment.md#verifying)).
- [ ] To roll back, run **Deploy** with the last good commit. To rebuild the
      current set, run **Compose**.

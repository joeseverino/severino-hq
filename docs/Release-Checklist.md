# Release Checklist

From a branch to a change live in production. `main` is protected; it changes
only by a merged pull request.

## 1. Before pushing

- [ ] `mise run check` passes.
- [ ] A change to templates or CSS passes `mise run browser` and has been
      walked in a real browser through the workflow it serves.
- [ ] A changed route, capability, SDK export or connection shape has its
      derived files regenerated: `mise run fix`.
- [ ] A new constraint on a table that holds rows has passed
      `manage.py constraint_preflight --path <database>` against production
      data.
- [ ] The diff names no private identifier, secret or extension.

## 2. The pull request

- [ ] One pull request per piece of work, with the repository's open
      dependency updates folded in.
- [ ] **Ready** passes. When it fails, its summary names each job that did
      not pass, and that job's log the error.
- [ ] `mise run check --release` (or `mise run check --release --remote` after
      the push) exits 0: every gate, the composed suite with the extensions
      required, and the deploy host read-only.

## 3. Merge and deploy

- [ ] After the merge, CI promotes the proven image and starts Compose.
- [ ] The commit's `production` deployment is *queued*, waiting for
      approval. Approve the Deploy run in the `production` environment. A
      newer release waiting supersedes an older one: reject the stale one.
- [ ] The deploy rehearses the release before it touches anything live
      (step 5 of [A deploy](Deployment.md#a-deploy)); its `Rehearsal:` line in
      the Deploy job's log says `result=pass`, or which check refused it.
- [ ] The Deploy job is green and the deployment is *success*. CI
      green is not live: only the Deploy job deploys.

## 4. If it is not live

- [ ] The failed job's log names the error; its fix is in
      [Deployment](Deployment.md#verifying).
- [ ] To roll back, run **Deploy** with the last good commit. To rebuild the
      current set, run **Compose**.

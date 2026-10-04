# Generated HQ clients

`npm --prefix scripts/openapi ci` installs the locked toolchain. Node 24.21.0
or newer runs the generated TypeScript directly with built-in type stripping.
`scripts/generate-clients.sh --write` refreshes all three generated files;
`--check` regenerates in a temporary directory and rejects any difference.
`scripts/check-openapi.sh` lints both contracts, checks client drift and runs
mocked runtime tests. This does not contact a deployed HQ.

Redocly 2.57.0 labels client generation experimental. Its pinned output and
runtime tests reduce upgrade risk; they do not make the upstream API stable.
The committed host-only contract generates these clients. Deployment-specific
extension operations require generation from that deployment's live document.

The TypeScript entry is `generated/hq.ts`; create/configure a client with the
server URL and bearer auth. Methods take grouped path/query/body/headers inputs.
Responses retain HQ's `{ok,data}` envelope, including awaiting-approval results;
non-success HTTP responses throw ApiError with status and body.

The repo-local CLI launcher is `node scripts/openapi/hq.mjs`. Set `HQ_API_URL`
and `HQ_API_TOKEN`; `--help` lists the generated commands. For example:

```sh
node scripts/openapi/hq.mjs listProjectsV2 --limit 10 --query example --dry-run
node scripts/openapi/hq.mjs projectUpdateV2 --idempotency-key example-retry-1 --json '{"target":"example","payload":{"name":"Example"}}' --dry-run
```

Dry runs perform no network request and redact bearer credentials. Writes get
one generated Idempotency-Key per logical invocation, stable across retries.
Provide `--idempotency-key` when retrying across separate CLI invocations.
Approval-held results require operator action; do not retry them as failures.
This launcher does not replace the external tools repository's `bin/hq`.

# Native controller and conventional HQ package layout

Status: proposed for the combined HQ PR; deployment approval is pending.

HQ's controller migration and developer-experience work must leave one shared
application contract and a repository where implementation ownership is clear.

Use Go for the infrastructure worker, with typed vendor responses and typed
failure classes. Remove the Python worker and its compatibility coercions.
Retain captured parity output as migration evidence only. Future behavior tests
exercise the Go implementation directly.

Organize Python under `hq/config`, `hq/platform` and `hq/domains`. Keep
`hq_sdk` as the supported top-level extension import surface. Move synthetic
fixtures and property tests under `tests`. Explicit Django app labels preserve
model identities, table names and migration dependencies. Existing host import
paths receive no compatibility packages; consumers import `hq_sdk`.

Use `pyproject.toml` and `uv.lock` for dependencies and tool configuration.
The image exports locked requirements and installs with required hashes.
Runtime state belongs under ignored `var/`; existing database data requires an
explicit path override rather than being silently abandoned during the move.

Emit API contracts from runtime declarations and derive consumers from those
documents. Pin OpenAPI 3.2.0, the version kin-openapi recognizes; move to 3.2.1
when it does. A patch release adds no features, so nothing is lost meanwhile. The generated TypeScript and repository-local CLI use
Redocly's experimental generator. All MCP input schemas and descriptions consume the live document's
`x-hq-mcp-tools` extension, emitted from declared service signatures. Resource
catalogs consume the document's already-emitted paths. This metadata describes
MCP-only tools explicitly, rather than claiming each is an HTTP operation.

Evidence: the moved tree preserves 23 app labels, 44 model/table identities
and 92 migration nodes; migration dry-run found no changes. The integrated
host and installed-plugin suites pass 4,081 tests after an approved test-only
correction to an inherited plugin assertion. Mypy passes 63 source files.
The root gate passes 3,322 host tests in each configured mode and
30 browser layout checks; the controller regeneration/vet/race gate passes.
A live localhost browser walk signs in, loads the self-hosted Scalar reference
and its document, and shows no JavaScript errors or phone horizontal overflow.

The substantive duplication findings are removed by sharing code: sorting and
transport policy use Go standard library calls and a provider-local helper, and
the NPM redirect and 404-host readers embed one host record resolved by one
helper. The structural classification excludes verified generated methods and
literal resource declarations; the structural bar holds with no baseline
addition. CodeQL is clean after correcting layout imports. The OpenAPI parser
dependency is kin-openapi 0.144.0 with unchanged generated clients.

Scorecard is clean. GO-2026-5932 covers only the x/crypto OpenPGP packages in
every release, so no version fixes it. `controller/osv-scanner.toml` records it
as not applicable, and `controller/scripts/check.sh` fails if an OpenPGP package
enters the controller, test or generator import graph. The gate is unchanged.

These results do not establish deployment readiness. Image construction, production constraint checks and edge shadow comparison remain
on hold at the operator's request.

## Built-ins and libraries weighed against HQ's own helpers

The rule: adopt the standard option when it is at least as good and leaves the
code easier to read and change. Each helper below was measured against its
replacement on 2026-10-04. None was swapped; one leftover was fixed.

Plurals keep `counted`. HQ is English-only with no catalog, so `ngettext`
would only pick one of two strings: it does not print the count, group its
thousands, spell a regular plural from a registry label, or refuse a phrase
given one form. `counted` does all four in under 40 lines, is part of the
`hq_sdk` contract, and has 95 Python calls and 49 template uses that would
each grow. Django's
`pluralize` agrees the noun and forgets the verb, which is what check
`hq.W101` exists to refuse, and `hq.E101` reads every literal phrase before a
page can raise on it. Neither check has a built-in equivalent.

Byte counts keep `human_bytes`, reached in templates through the `bytes`
filter. `filesizeformat` says "38.0 MB" and "512 bytes" where the controller's
readings and the connection summaries say "38 MB" and "512 B", so adopting it
would change every rendered count to match the less terse form. No call to
`filesizeformat` remains; architecture tests hold that for templates and for
Python.

Serializers stay plain functions. The 28 of them hold 485 keys in 767 lines,
and 210 of those keys copy a same-named attribute, which is all
`from_attributes` reads unaided. The rest are slugs of relations,
sensitivity-gated document ids, money as strings and derived values, each a
validator or serializer in pydantic. `serialize_expense` rewritten that way
gives the same output in 34 lines against 22. The estimate of 600 lines saved
does not hold. The API document types a list as open objects today, so
response models would not be a second source of truth, but adding them is a
contract feature (the document and every generated client change) and belongs
to the contract work, routed through the document's existing type hoisting.

Audit keeps its own field diff. About 100 of the 408 lines in
`hq/platform/core/audit.py` snapshot and diff fields; the rest is what the
plan already keeps: operation and connection attribution, redaction,
observation-only saves that are not events, required events, retention.
django-auditlog and django-simple-history each write to tables of their own,
so either would add a second audit store beside `AuditLog`, which the login
throttle and the activity views read. Neither release declares Django 6.1:
django-auditlog 3.4.1 stops at Django 5.2 and Python 3.13, and
django-simple-history 3.13.0 at Django 6.0.

Settings keep `env_bool`, `env_int`, `env_list` and `env_secret`, 45 lines
with no dependency. django-environ 0.14.0 would differ where HQ is deliberate:
a malformed integer raises instead of falling back, list items are not
stripped, and its file-aware mapping lets `NAME_FILE` silently win over `NAME`
and keeps the trailing newline, where `env_secret` refuses both and strips. Its
`read_env` is a line parser with its own quoting rules, not the shell quoting
of the file 1Password renders. Every integer
setting now goes through `env_int`: one with a `minimum` refuses to start when
the value is malformed or out of range, the rest fall back to their default,
and a test keeps a raw `int(os.environ...)` out of settings.

Jobs stay on a thread with a `Job` row. Django 6.1 ships `django.tasks` with
an immediate backend, which runs the work inside the request, and a dummy
backend, which never runs it. Leaving the request needs a third-party backend
and a worker process, and a task result carries a status and a return value,
not progress notes, a heartbeat, one live job per kind, or a lost job recorded
as lost.

## Gates and tool pins declared once

Declare every gate and every tool pin once, in `mise.toml` and `mise.lock`.
A gate is a task named `<job>:<gate>`; each CI job runs the aggregate of its
name (`mise run -c checks`) and `mise run ci` runs the same tasks on a
developer's machine. A gate is added in `mise.toml`, never in a workflow or a
script, so the pipeline and a local run cannot disagree about what is checked.

Tool versions live in `mise.toml` and their checksums in `mise.lock`. The
lockfile reproduced the checksums previously recorded by hand, and adds
provenance verification of each download. `scripts/toolchain.env` keeps only the
facts that are not tools: the Python matrix, the coverage floor, the runner
image, the Cordon commit and the failure-log recipient. Python dependencies
come from `uv.lock` through `uv run --locked`; no gate names an interpreter path
or needs a virtualenv made by hand, and `mise run tests` walks every Python the
matrix declares.

Removed: `scripts/ci-local.sh`, `scripts/check.sh`, `scripts/check-fast.sh`,
`scripts/install-scan-tools.sh`, `scripts/coverage-badge.sh` and
`scripts/dev.env.example`. They are `mise run ci`, `mise run check`,
`mise run fast`, `mise install`, the `checks:badges` gate (the README badge
states the declared floor) and `scripts/mise.local.example.toml`.
Developer-local settings moved from `.env.dev` to a gitignored
`mise.local.toml`. The hand-kept list of shell files is gone:
`scripts/shell-sources.sh` derives it from the tree and the suites are
`scripts/test-*.sh`. `controller/scripts/check.sh` stays; `controller:check`
runs it. No compatibility wrappers remain.

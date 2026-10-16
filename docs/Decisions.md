# Decisions

Decisions that still shape the code, with the reason for each. A change that
reverses one updates this page in the same commit.

## A Go controller

The infrastructure worker is Go (`controller/`), with typed vendor responses
and typed failure classes. It runs from the exact deployed image as a
short-lived container and reaches HQ only over the bridge
([Architecture](Architecture.md#infrastructure-control-plane)). The host's own
programs, `hq-host` and `hq-secrets`, are Go in the same module, so root runs
static binaries the image built rather than shell.

## One package layout

Python lives under `hq/config`, `hq/platform` and `hq/domains`; `hq_sdk` stays
the top-level import surface for extensions, and synthetic fixtures and
property tests live under `tests/`. Explicit Django app labels keep model
identities, table names and migration dependencies independent of package
paths, and no compatibility package re-exports an old import path.
Dependencies and tool configuration are `pyproject.toml` and `uv.lock`; the
image installs locked requirements with hashes.

## Contracts are emitted, consumers derived

API contracts are emitted from runtime declarations, and every consumer is
derived from the document: the OpenAPI 3.2.0 description (the version
kin-openapi recognizes), the generated TypeScript client and repository CLI
(Redocly's generator), and the MCP tool schemas, read from the document's
`x-hq-mcp-tools` extension. MCP-only tools are described as such rather than
as HTTP operations.

## Gates and pins declared once

Every gate is a `mise` task and every tool pin is in `mise.toml` and
`mise.lock`; a CI job runs the aggregate of its name, so the pipeline and a
local run cannot disagree ([Development](Development.md)).

## Built-ins weighed against HQ's own helpers

The rule: adopt the standard option when it is at least as good and leaves the
code easier to read and change. Each helper below was measured against its
replacement, and kept for the reason given.

Plurals keep `counted`. HQ is English-only with no catalog, so `ngettext`
would only pick one of two strings: it does not print the count, group its
thousands, spell a regular plural from a registry label, or refuse a phrase
given one form. `counted` does all four in under 40 lines, is part of the
`hq_sdk` contract, and has 95 Python calls and 49 template uses that would
each grow. Django's
`pluralize` agrees the noun and forgets the verb, which the architecture test
`InterfaceTextTests` refuses, and the same pass reads every literal phrase
before a page can raise on it. Neither rule has a built-in equivalent.

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
setting goes through `env_int`: one with a `minimum` refuses to start when
the value is malformed or out of range, the rest fall back to their default,
and a test keeps a raw `int(os.environ...)` out of settings.

Jobs stay on a thread with a `Job` row. Django 6.1 ships `django.tasks` with
an immediate backend, which runs the work inside the request, and a dummy
backend, which never runs it. Leaving the request needs a third-party backend
and a worker process, and a task result carries a status and a return value,
not progress notes, a heartbeat, one live job per kind, or a lost job recorded
as lost.

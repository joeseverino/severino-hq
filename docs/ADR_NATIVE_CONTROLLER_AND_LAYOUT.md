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
documents. Use OpenAPI 3.2.0 because the selected parser supports it; 3.2.1 was
rejected by that parser. The generated TypeScript and repository-local CLI use
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

The two substantive sorting/transport-policy duplication findings have been
removed using Go standard library calls and a provider-local helper. The updated
structural classification excludes verified generated methods and literal
resource declarations, while ambiguous typed projections remain gated.
CodeQL is clean after correcting layout imports. The OpenAPI parser dependency
is updated to kin-openapi 0.144.0 with unchanged generated clients. Scorecard
still reports GO-2026-5932 for the x/crypto module: its OpenPGP packages are
absent from the resolved controller, test and generator import graph, but the
existing strict module-level gate remains failed.

These results do not establish deployment readiness. Image construction, production constraint checks and edge shadow comparison remain
on hold at the operator's request.

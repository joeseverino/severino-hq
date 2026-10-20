# Structural Gate

The gate retains the executable near-duplicate baseline; file size is
`FileSizeTests`, a fixed ceiling in the suite.
Source-derived classifications remove only these forms from the executable-pair
check; there are no pair, symbol, or source-path exemption lists.

* Generated Go: the parser finds a `// Code generated ... DO NOT EDIT.` comment
  before the package statement. The generated file, adjacent generation source,
  generator config and schema must be tracked. An adjacent `go:generate go tool
  oapi-codegen -config CONFIG SCHEMA` directive must resolve to existing files
  inside the checkout; the config must have one simple top-level `output` scalar
  resolving to this file. This is provenance correspondence, not proof of
  freshness or authenticity. `controller/scripts/check.sh` regeneration with no
  diff remains mandatory in CI and `mise run ci`. A marker alone never qualifies.
* Python immutable metadata: a module-level undecorated function has no runtime
  arguments and exactly one return statement. Its returned tree contains only
  constants, names, attribute references, tuples, and calls to the imported
  ResourceSpec/SearchDefinition contract constructors. Relative imports are
  resolved. Constructor rebinding, lists/dicts, operators, control flow, starred
  arguments and computed calls do not qualify. These are syntactic declarations;
  the classifier does not claim the referenced objects are pure at runtime.
* Go tiny typed delegation: exactly three statements acquire a string/error
  result from a resolved local producer, return the error unchanged, and forward
  to a shared call. Producer arguments must be identifiers; receiver identity is
  checked for methods. The pair must have different producers, different producer
  type signatures and different wrapper type signatures, but identical forwarding
  syntax after credential-name normalization. Parameter-name-only differences
  do not count. Ambiguous method names, closures/indexing, spread arguments and
  extra policy branches do not qualify. Forward arguments permit identifiers,
  selectors, constants, literal constructors and a literal/constant string prefix
  concatenated with a string parameter. Nested calls qualify only when resolved
  to an unshadowed same-file nonmethod with one string parameter and a sole return
  of a map[string]string literal. Map keys must be literal strings, and values
  literal/constant strings, that parameter, or prefix-plus-parameter. Nested calls,
  operators other than that concatenation, side effects and control flow reject
  the decorator. Parameter/credential shadowing and constant/parameter collisions
  reject ambiguous resolution. This separates orchestration shape; it
  does not prove producer or shared-helper purity.

The AST inspector uses Go's standard-library parser, never imports or executes
provider code. Python uses `ast`. Missing, unreadable, malformed or escaped source
paths and missing Go graph symbols fail the classification command. Unresolved or
unsupported executable patterns remain in the similarity gate; they do not become
an inferred exemption. Typed NPM projection loops remain gated in this iteration.

The inspector is `controller/cmd/structural-inspector`, held by the
controller's gate like the rest of its module. The adversarial tests are
`mise run checks:source tools.structural`, which builds it.

## Asking the graph

With the repository indexed in a code knowledge graph, ask:

| Question | Query | Bar |
| --- | --- | --- |
| Did I re-implement something? | `MATCH (a)-[r:SIMILAR_TO]->(b) RETURN a.file_path, b.file_path, a.name, b.name` | no new executable pair outside tests; the classifications above |
| Hidden O(n²)? | `MATCH (f) WHERE (f:Function OR f:Method) AND f.linear_scan_in_loop >= 1 RETURN f.qualified_name, f.linear_scan_in_loop` | every hit bounded by a fixed or small input |
| Did I tangle the call graph? | `get_architecture(aspects: ["cycles"])` | no new confirmed cycle |

Write the queries exactly as given, and mind two traps in this engine.
`is_test` is **not** reliable: test classes carry `is_test: false`, so a filter
on it silently counts the whole suite. And `NOT <prop> CONTAINS "..."` returns
**no rows at all** rather than the complement, so a negated filter reads as a
clean bar when it measured nothing. Filter positively, or return the rows and
exclude test paths by eye.

Re-index after a change and re-run them; a result that moved the wrong way is a
finding whether or not the suite is green.

Confirm a cycle before believing it. Python's `.get()` on a dict resolves to any
class method named `get`, so view classes turn up in cycles they have nothing to
do with: read the function and check it really calls into the loop. The same
caution applies to `trace_path`: its first hop is exact, deeper hops resolve
generic names (`get`, `handle`, `search`) optimistically. Use it for blast
radius, verify before acting on a three-hop claim.

Index the checkout you work in before asking the graph anything; a worktree is
its own checkout.

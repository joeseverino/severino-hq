# Structural Gate

The gate fails a near-duplicate pair of executable code that its baseline
does not list; file size is `FileSizeTests`, a fixed ceiling in the suite.
The forms below, classified from the source, leave the pair check. There is no
list of exempt pairs, symbols or paths.

A pair is near when the graph's MinHash fingerprints of the two functions
agree on 85% of their 64 slots, in the same language, outside tests (a test
file, or a Python function named `test_*`). The graph fingerprints a body of
at least 30 tokens from its token-type trigrams, with names and literals
normalized away, so a pair is two functions of the same shape. The bar reads
every fingerprint and compares them itself: the graph draws its own
`SIMILAR_TO` edges only at 95%, a constant of the pinned release, where every
pair outside tests is generated code. Below the bar's reach: a repeated
fragment inside larger functions, and an idiom of a line or two (a lookup
repeated across sites, a two-line override). Those are held by an
architecture test that names the one place the rule lives.

* Generated Go: the parser finds a `// Code generated ... DO NOT EDIT.` comment
  before the package statement. The generated file, adjacent generation source,
  generator config and schema must be tracked. An adjacent `go:generate go tool
  oapi-codegen -config CONFIG SCHEMA` directive must resolve to existing files
  inside the checkout; the config must have one simple top-level `output` scalar
  resolving to this file. That shows where the file came from; whether it is
  current is `controller/scripts/check.sh`, which regenerates every generated
  file and fails on a diff, in CI and `mise run lint`. A marker alone never
  qualifies.
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
  the nested call. Parameter/credential shadowing and constant/parameter
  collisions reject ambiguous resolution. The classifier reads the shape of
  the orchestration and makes no claim about the purity of the producer or
  the shared helper.
* Python dict projections over disjoint keys: each function (a method found
  in its class) has, after an optional docstring, one return of a dict literal
  whose keys are all literal strings, and the two key sets share none. A
  shared key, `**` unpacking, an empty literal or any other statement does not
  qualify. Two such functions share only the literal's shape.
* Go delegation to one shared function: in the same file, each body is a
  single return of a call (an instantiated generic included) of the same
  unshadowed nonmethod declared there. The logic both run is already one
  function; what differs is what they hand it.
* Go switch tables over disjoint cases: each body is variable declarations
  without values, `:=` of a literal, one switch, and a final return identical
  in both. Every clause sets one and the same variable from a literal or a
  call of a named function on identifiers and literals. Case expressions name
  a declared variable by its type, so a renamed copy reads the same, and the
  two case sets share none.

The AST inspector uses Go's standard-library parser, never imports or executes
provider code. Python uses `ast`. Missing, unreadable, malformed or escaped source
paths and missing Go graph symbols fail the classification command. Unresolved or
unsupported executable patterns stay in the similarity gate. Typed projection
loops over Nginx Proxy Manager's records stay gated too.

The inspector is `controller/cmd/structural-inspector`, held by the
controller's gate like the rest of its module, its own tests among them. The
adversarial tests of the pairs are `mise run lint:source tools.structural`,
which builds it.

## Asking the graph

With the repository indexed in a code knowledge graph, ask:

| Question | Query | Bar |
| --- | --- | --- |
| Did I re-implement something? | `mise run lint:structural`, which compares every fingerprint (`f.fp`) at the bar's threshold; the graph's `SIMILAR_TO` edges hold only the pairs at 95% | no new executable pair outside tests; the classifications above |
| Hidden O(n²)? | `MATCH (f) WHERE (f:Function OR f:Method) AND f.linear_scan_in_loop >= 1 RETURN f.qualified_name, f.linear_scan_in_loop` | every hit bounded by a fixed or small input |
| Did I tangle the call graph? | `get_architecture(aspects: ["cycles"])` | no new confirmed cycle |

Write the queries exactly as given, and mind the traps in this engine. The
generated `*.gen.go` files are near-duplicates of each other by design, so a
`SIMILAR_TO` query returns hundreds of rows until it is scoped positively to
the paths under review. A query answers in pages, and each page runs it
again: ask for `max_output_tokens` large enough for one page, or a whole-graph
read takes minutes. The graph cannot answer "what has no callers": an `OPTIONAL
MATCH` comes back empty and `max_degree` counts both directions, so dead code
is found by diffing the defined names against the referenced ones with `rg`.
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

# Structural Gate

The gate fails a near-duplicate pair of executable code that its baseline
does not list. File size is `FileSizeTests`, held to the ceiling, the files it covers (`SEVERINO_FILE_LINE_HELD`) and the exemptions `mise.toml` declares (`SEVERINO_FILE_LINE_*`).

The forms below, classified from the source, leave the pair check. There is no
list of exempt pairs, symbols or paths.

A pair is near when the graph's MinHash fingerprints of the two functions
agree on 85% of their 64 slots. Both are in the same language and outside
tests (a test file, or a Python function named `test_*`).

The graph fingerprints a body of at least 30 tokens from its token-type
trigrams, with names and literals normalized away. So a pair is two functions
of the same shape.

The bar reads every fingerprint and compares them itself. The graph draws its
own `SIMILAR_TO` edges only at 95%, a constant of the pinned release, where
every pair outside tests is generated code.

Two things are below the bar's reach: a repeated fragment inside larger
functions, and an idiom of a line or two (a lookup repeated across sites, a
two-line override). Those are held by an architecture test that names the one
place the rule lives.

A flagged pair is resolved by moving the shape both repeat into one function
they call. That shape is usually a constructor call with many keyword
arguments (a model's `create`, a `through(...)` row, a log call), since it
holds most of the trigrams. A helper that shares only the loop or the `try`
around it leaves the pair near.

## The forms that leave the pair check

| Form | Language | In short |
| --- | --- | --- |
| [Generated Go](#generated-go) | Go | a generated file whose generator is tracked |
| [Immutable metadata](#immutable-metadata) | Python | one return of contract constructors |
| [Tiny typed delegation](#tiny-typed-delegation) | Go | three statements that acquire, check and forward |
| [Dict projections over disjoint keys](#dict-projections-over-disjoint-keys) | Python | one dict literal, no key shared |
| [Delegation to one shared function](#delegation-to-one-shared-function) | Go | one return of the same call |
| [Switch tables over disjoint cases](#switch-tables-over-disjoint-cases) | Go | one switch, no case shared |

### Generated Go

The parser finds a `// Code generated ... DO NOT EDIT.` comment before the
package statement. A marker alone never qualifies.

The generated file, adjacent generation source, generator config and schema
must be tracked. An adjacent `go:generate go tool oapi-codegen -config CONFIG
SCHEMA` directive must resolve to existing files inside the checkout. The
config must have one simple top-level `output` scalar resolving to this file.

That shows where the file came from. Whether it is current is
`controller/scripts/check.sh`, which regenerates every generated file and fails
on a diff, in CI and `mise run lint`.

### Immutable metadata

A module-level undecorated Python function has no runtime arguments and
exactly one return statement. Its returned tree contains only constants,
names, attribute references, tuples, and calls to the imported
ResourceSpec/SearchDefinition contract constructors. Relative imports are
resolved.

Constructor rebinding, lists and dicts, operators, control flow, starred
arguments and computed calls do not qualify.

These are syntactic declarations. The classifier does not claim the referenced
objects are pure at runtime.

### Tiny typed delegation

Exactly three Go statements acquire a string/error result from a resolved
local producer, return the error unchanged, and forward to a shared call.

| Part | What the pair must have |
| --- | --- |
| Producer arguments | identifiers only; receiver identity is checked for methods |
| Producers | different, with different type signatures |
| Wrappers | different type signatures; parameter-name-only differences do not count |
| Forwarding | identical syntax after credential-name normalization |

Ambiguous method names, closures or indexing, spread arguments and extra
policy branches do not qualify.

Forward arguments permit identifiers, selectors, constants, literal
constructors, and a literal or constant string prefix concatenated with a
string parameter.

A nested call qualifies only when it resolves to an unshadowed same-file
nonmethod with one string parameter and a sole return of a `map[string]string`
literal. Map keys must be literal strings. Values must be literal or constant
strings, that parameter, or prefix-plus-parameter. Nested calls, operators
other than that concatenation, side effects and control flow reject the nested
call.

Parameter or credential shadowing, and constant/parameter collisions, reject
ambiguous resolution. The classifier reads the shape of the orchestration. It
makes no claim about the purity of the producer or the shared helper.

### Dict projections over disjoint keys

Each Python function (a method found in its class) has, after an optional
docstring, one return of a dict literal. Its keys are all literal strings, and
the two key sets share none. Two such functions share only the literal's
shape.

A shared key, `**` unpacking, an empty literal or any other statement does not
qualify.

### Delegation to one shared function

In the same Go file, each body is a single return of a call (an instantiated
generic included) of the same unshadowed nonmethod declared there. The logic
both run is already one function; what differs is what they hand it.

### Switch tables over disjoint cases

Each Go body is variable declarations without values, `:=` of a literal, one
switch, and a final return identical in both. Every clause sets one and the
same variable from a literal, or from a call of a named function on
identifiers and literals.

Case expressions name a declared variable by its type, so a renamed copy reads
the same. The two case sets share none.

## How the bar runs

The AST inspector uses Go's standard-library parser, and never imports or
executes provider code. Python uses `ast`.

| Input | Verdict |
| --- | --- |
| Missing, unreadable, malformed or escaped source paths | the classification command fails |
| Missing Go graph symbols | the classification command fails |
| Unresolved or unsupported executable patterns | stay in the similarity gate |
| Typed projection loops over Nginx Proxy Manager's records | stay in the similarity gate |

The inspector is `controller/cmd/structural-inspector`, held by the
controller's gate like the rest of its module, its own tests among them. The
adversarial tests of the pairs are `mise run lint:source tools.structural`,
which builds it.

The bar keeps the pairs it found under `var/structural`, keyed by a digest of
what they depend on:

- the tree git would write from every file it sees, staged or not (the
  baseline aside);
- which files are tracked;
- the graph and Go releases.

A run on inputs already read answers from there and compares that answer with
the baseline. So the verdict is the one a fresh graph gives.

The bar prints its verdict and a count of the pairs a classification explains.
`mise run lint:structural --pairs` first prints every pair outside tests, one
per line and sorted, with the classification that explains it or
`unexplained`. Then it gives the same verdict. So a change to
`tools/structural/classify.py` is checked by diffing that list before and
after.

## Asking the graph

With the repository indexed in a code knowledge graph, ask:

| Question | Query | Bar |
| --- | --- | --- |
| Did I re-implement something? | `mise run lint:structural`, which compares every fingerprint (`f.fp`) at the bar's threshold; the graph's `SIMILAR_TO` edges hold only the pairs at 95% | no new executable pair outside tests; the classifications above |
| Hidden O(n²)? | `MATCH (f) WHERE (f:Function OR f:Method) AND f.linear_scan_in_loop >= 1 RETURN f.qualified_name, f.linear_scan_in_loop` | every hit bounded by a fixed or small input |
| Did I tangle the call graph? | `get_architecture(aspects: ["cycles"])` | no new confirmed cycle |

Index the checkout you work in before asking the graph anything; a worktree is
its own checkout. Write the queries exactly as given, and mind the traps in
this engine:

| Trap | What to do |
| --- | --- |
| The generated `*.gen.go` files are near-duplicates of each other by design, so a `SIMILAR_TO` query returns hundreds of rows | scope it positively to the paths under review |
| A query answers in pages, and each page runs it again | ask for `max_output_tokens` large enough for one page, or a whole-graph read takes minutes |
| The graph cannot answer "what has no callers": an `OPTIONAL MATCH` comes back empty and `max_degree` counts both directions | find dead code by diffing the defined names against the referenced ones with `rg` |
| `is_test` is **not** reliable: test classes carry `is_test: false`, so a filter on it silently counts the whole suite | filter on paths |
| `NOT <prop> CONTAINS "..."` returns **no rows at all** rather than the complement, so a negated filter reads as a clean bar when it measured nothing | filter positively, or return the rows and exclude test paths by eye |
| `INHERITS` edges stop at the repository boundary, so the classes HQ extends from Django, pydantic or the standard library are not in the graph | `rg -o '^class \w+\(([A-Za-z_.]+)'` lists them |
| Python's `.get()` on a dict resolves to any class method named `get`, so view classes turn up in cycles they have nothing to do with | confirm a cycle before believing it: read the function and check it really calls into the loop |
| `trace_path`'s first hop is exact, but deeper hops resolve generic names (`get`, `handle`, `search`) optimistically | use it for blast radius, and verify before acting on a three-hop claim |

Re-index after a change and re-run them. A result that moved the wrong way is
a finding whether or not the suite is green.

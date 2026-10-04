# Structural similarity policy

The gate retains the executable near-duplicate baseline and largest-file limits.
Source-derived classifications remove only these forms from the executable-pair
check; there are no pair, symbol, or source-path exemption lists.

* Generated Go: the parser finds a `// Code generated ... DO NOT EDIT.` comment
  before the package statement. The generated file, adjacent generation source,
  generator config and schema must be tracked. An adjacent `go:generate go tool
  oapi-codegen -config CONFIG SCHEMA` directive must resolve to existing files
  inside the checkout; the config must have one simple top-level `output` scalar
  resolving to this file. This is provenance correspondence, not proof of
  freshness or authenticity. `controller/scripts/check.sh` regeneration with no
  diff remains mandatory in CI and ci-local. A marker alone never qualifies.
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

Run `go build -o /tmp/structural-inspector scripts/structural_go.go`, then
`STRUCTURAL_INSPECTOR=/tmp/structural-inspector python3 scripts/test_structural_classify.py`
for focused adversarial tests. `STRUCTURAL_REVIEW_ROOT` optionally adds read-only
checks of the actual reviewed declaration, generated, delegation and NPM pairs.

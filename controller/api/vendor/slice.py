"""Cut the schemas a vendor's provider decodes out of its upstream spec.

The operations are the ``include-operation-ids`` of the vendor directory's
``oapi-codegen.yaml``, and what the provider decodes and sends is named in its
``slice.toml``, each key described where it is read. Run from the vendor
directory against the pinned upstream file its UPSTREAM records:
``python3 ../slice.py /path/to/openapi.json > openapi.slice.json``.
"""

import copy
import json
import sys
import tomllib
from pathlib import Path

METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")
SCHEMAS = "#/components/schemas/"


def operation_ids(config: Path) -> tuple[str, ...]:
    """The ``include-operation-ids`` list of an oapi-codegen config."""

    found: list[str] = []
    listing = False
    for line in config.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped == "include-operation-ids:":
            listing = True
        elif listing and stripped.startswith("- "):
            found.append(stripped[2:].strip())
        elif listing and stripped:
            break
    if not found:
        raise SystemExit(f"{config} names no include-operation-ids")
    return tuple(found)


def refs(node, found):
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/"):
            found.add(ref)
        for value in node.values():
            refs(value, found)
    elif isinstance(node, list):
        for value in node:
            refs(value, found)


def strip_keywords(node, keywords):
    if isinstance(node, dict):
        for key in keywords:
            if not isinstance(node.get(key), dict):
                node.pop(key, None)
        for value in node.values():
            strip_keywords(value, keywords)
    elif isinstance(node, list):
        for value in node:
            strip_keywords(value, keywords)


def drop_media_examples(node):
    """Drop ``examples`` from every media type object (a ``content`` entry)."""

    if isinstance(node, dict):
        for media in (node.get("content") or {}).values() if isinstance(node.get("content"), dict) else ():
            if isinstance(media, dict):
                media.pop("examples", None)
        for value in node.values():
            drop_media_examples(value)
    elif isinstance(node, list):
        for value in node:
            drop_media_examples(value)


def component(spec, ref):
    _, _, kind, name = ref.split("/", 3)
    return kind, name, spec["components"][kind][name]


def reachable(spec, node):
    """Every component ref reachable from a node, transitively."""

    pending, seen = set(), set()
    refs(node, pending)
    while pending:
        ref = pending.pop()
        if ref not in seen:
            seen.add(ref)
            refs(component(spec, ref)[2], pending)
    return seen


def success(operation):
    return {code: answer for code, answer in operation.get("responses", {}).items() if code.startswith("2")}


def parameters(spec, item, operation, queries):
    """The route's path parameters as plain strings, then the query parameters
    the provider sends, in the order it names them, so the generated client
    builds the request. A query parameter is a pointer, its schema inlined to
    carry that, so one a call leaves unset is not sent."""

    found, query = [], {}
    for declared in (*item.get("parameters", ()), *operation.get("parameters", ())):
        parameter = component(spec, declared["$ref"])[2] if "$ref" in declared else declared
        if parameter.get("in") == "path":
            found.append({"name": parameter["name"], "in": "path", "required": True, "schema": {"type": "string"}})
        elif parameter.get("in") == "query":
            query[parameter["name"]] = parameter
    for name in queries.get(operation["operationId"], ()):
        if name not in query:
            raise SystemExit(f"{operation['operationId']} takes no query {name!r}")
        parameter = copy.deepcopy(query[name])
        schema = parameter.get("schema", {})
        if "$ref" in schema:
            schema = copy.deepcopy(component(spec, schema["$ref"])[2])
        parameter["schema"] = {**schema, "x-go-type-skip-optional-pointer": False}
        found.append(parameter)
    return found


def built_on(variant):
    """The components a union variant names directly or as allOf parts."""

    return {part["$ref"].removeprefix(SCHEMAS) for part in (variant, *variant.get("allOf", ())) if "$ref" in part}


def narrow(name, schema, kept):
    """Keep only the named properties of a schema and its inline allOf parts,
    or, for a oneOf or anyOf, only the variants built on the named components."""

    kept = set(kept)
    found = set()
    unions = [key for key in ("oneOf", "anyOf") if key in schema]
    for key in unions:
        schema[key] = [variant for variant in schema[key] if built_on(variant) & kept]
        found |= {part for variant in schema[key] for part in built_on(variant)}
    if not unions:
        for part in (schema, *(part for part in schema.get("allOf", ()) if "$ref" not in part)):
            if "properties" in part:
                found |= part["properties"].keys() & kept
                part["properties"] = {key: value for key, value in part["properties"].items() if key in kept}
                if "required" in part:
                    part["required"] = [key for key in part["required"] if key in kept]
    if missing := kept - found:
        raise SystemExit(f"keep names what {name} does not have: {sorted(missing)}")


def names(variant, decoded):
    """Whether a union variant names one of the decoded schemas itself."""

    found = set()
    refs(variant, found)
    return bool(found & {SCHEMAS + name for name in decoded})


def answer(spec, op_id, upstream, decoded, body):
    """A success answer: its JSON body when the generated client decodes it,
    a union narrowed to the one variant naming a schema the provider decodes,
    which stands in for the union; else its description alone."""

    if body and "$ref" in upstream:
        upstream = component(spec, upstream["$ref"])[2]
    kept = {"description": upstream.get("description", "")}
    media = upstream.get("content", {}).get("application/json")
    if not body or media is None:
        return kept
    schema = copy.deepcopy(media["schema"])
    for key in ("oneOf", "anyOf"):
        if key in schema:
            variants = [variant for variant in schema[key] if names(variant, decoded)]
            if len(variants) != 1:
                raise SystemExit(f"{op_id} answers {len(variants)} variants it decodes")
            schema = variants[0]
    kept["content"] = {"application/json": {"schema": schema}}
    return kept


def slice_operation(spec, item, operation, settings, roots):
    """Keep an operation's route contract and validate its decoded models."""

    op_id = operation["operationId"]
    decodes, sends, encodes = settings["decodes"], settings["sends"], settings["encodes"]
    answers = success(operation)
    decoded = decodes.get(op_id, ())
    # An operation keeps its path, method, the query it sends and its success
    # statuses. Its answer bodies go where the provider reads its own
    # envelopes, and stay where the generated client decodes them.
    kept = {
        "operationId": op_id,
        "parameters": parameters(spec, item, operation, settings["queries"]),
        "responses": {
            code: answer(spec, op_id, upstream, decoded, settings["answers"]) for code, upstream in answers.items()
        },
    }
    upstream = reachable(spec, answers)
    for name in decoded:
        if SCHEMAS + name not in upstream:
            raise SystemExit(f"{op_id} answers no {name}")
        roots.add(SCHEMAS + name)
    if op_id in sends | encodes:
        if "requestBody" not in operation:
            raise SystemExit(f"{op_id} sends no request body")
        kept["requestBody"] = copy.deepcopy(operation["requestBody"])
    if op_id in encodes:
        kept["requestBody"]["content"] = {"application/json": {"schema": {}}}
    return kept


def slice_paths(spec, wanted, settings):
    """Select declared operations and collect their decoded schema roots."""

    # [decodes]: operation id = the component schemas the provider decodes from
    # its answer, each reachable from the operation's success response
    # upstream; an operation with none is decoded into the provider's own type,
    # or not at all. sends: the operation ids whose request body the provider
    # sends as the generated type. encodes: the operation ids whose request
    # body the provider encodes from a type of its own, kept as any JSON.
    # Every other request body goes. [queries]: operation id = the query
    # parameters the provider sends; the rest go. answers: true keeps each
    # success answer's JSON body, so the generated client decodes it.
    read = {
        "decodes": settings.get("decodes", {}),
        "sends": set(settings.get("sends", ())),
        "encodes": set(settings.get("encodes", ())),
        "queries": settings.get("queries", {}),
        "answers": settings.get("answers", False),
    }
    named = read["decodes"].keys() | read["sends"] | read["encodes"] | read["queries"].keys()
    if stray := named - wanted:
        raise SystemExit(f"slice.toml names operations oapi-codegen.yaml does not: {sorted(stray)}")
    roots = set()
    paths = {}
    for route, item in spec["paths"].items():
        for method, operation in item.items():
            if method not in METHODS or operation.get("operationId") not in wanted:
                continue
            wanted.discard(operation["operationId"])
            paths.setdefault(route, {})[method] = slice_operation(spec, item, operation, read, roots)
    if wanted:
        raise SystemExit(f"operations missing upstream: {sorted(wanted)}")
    # drop-examples: true drops media-type examples and the components only they use.
    if settings.get("drop-examples"):
        drop_media_examples(paths)
    return paths, roots


def slice_components(spec, paths, roots, settings):
    """Follow retained schema references after applying property narrowing."""

    # [keep]: component = the properties the provider reads; the rest go, with
    # what only they referenced. For a oneOf or anyOf, the components whose
    # variants stay. Every component the kept ones reference stays, transitively.
    keep = settings.get("keep", {})
    pending, seen = set(roots), set()
    refs(paths, pending)
    components = {}
    while pending:
        ref = pending.pop()
        if ref in seen:
            continue
        seen.add(ref)
        kind, name, body = component(spec, ref)
        body = copy.deepcopy(body)
        if kind == "schemas" and name in keep:
            narrow(name, body, keep[name])
        if settings.get("drop-examples"):
            drop_media_examples(body)
        components.setdefault(kind, {})[name] = body
        refs(body, pending)
    if stale := keep.keys() - components.get("schemas", {}).keys():
        raise SystemExit(f"keep names components the slice does not hold: {sorted(stale)}")
    return components


def name_components(components, names):
    """Apply explicit generated names only to retained components."""

    # [go-names]: "kind/name" (kind defaults to schemas) = the Go name for a kept
    # component whose generated name collides with an inline enum or alias
    # elsewhere in the slice.
    for key, go_name in names.items():
        kind, _, name = key.rpartition("/")
        kind = kind or "schemas"
        if name not in components.get(kind, {}):
            raise SystemExit(f"go-names names a component the slice does not hold: {key}")
        components[kind][name] = {**components[kind][name], "x-go-name": go_name}
    for kind in components:
        components[kind] = dict(sorted(components[kind].items()))


def main(path):
    vendor = Path.cwd()
    settings = tomllib.loads((vendor / "slice.toml").read_text(encoding="utf-8"))
    with Path(path).open(encoding="utf-8") as handle:
        spec = json.load(handle)
    # The spec's servers stay, so the provider's base URL is the vendor's
    # statement rather than a literal.
    if not spec.get("servers"):
        raise SystemExit("upstream names no servers")
    wanted = set(operation_ids(vendor / "oapi-codegen.yaml"))
    paths, roots = slice_paths(spec, wanted, settings)
    components = slice_components(spec, paths, roots, settings)
    name_components(components, settings.get("go-names", {}))
    # strip: keywords dropped everywhere they are not an object; ones that change
    # no decoded shape and that upstream allOf parts disagree on, which
    # oapi-codegen refuses to merge. Without format a timestamp stays the exact
    # string the vendor sent.
    keywords = tuple(settings.get("strip", ()))
    strip_keywords(paths, keywords)
    strip_keywords(components, keywords)
    slice_spec = {
        "openapi": spec["openapi"],
        "info": {"title": spec["info"]["title"], "version": spec["info"]["version"]},
        "servers": spec["servers"],
        "paths": dict(sorted(paths.items())),
        "components": dict(sorted(components.items())),
    }
    json.dump(slice_spec, sys.stdout, indent=1, sort_keys=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main(sys.argv[1])

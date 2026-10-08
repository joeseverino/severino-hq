"""Cut the schemas a vendor's provider decodes out of its upstream spec.

The operations are the ``include-operation-ids`` in the vendor directory's
``oapi-codegen.yaml``: the calls the provider makes, so the slice and the
generator name them once. Each operation keeps its path, method and success
statuses; its parameters and response bodies go, because only models are
generated and the provider builds its own requests and reads its own
envelopes. The spec's ``servers`` stay, so the provider's base URL is the
vendor's statement rather than a literal. What the provider decodes and sends is named in ``slice.toml``
beside them, and the slice keeps those schemas and every component they
reference, transitively. Run from the vendor directory against the pinned
upstream file recorded in its UPSTREAM:

    python3 ../slice.py /path/to/openapi.json > openapi.slice.json

slice.toml:
    [decodes]      operation id = the component schemas the provider decodes
                   from its answer; each must be reachable from the
                   operation's success response upstream. An operation with
                   none is decoded into the provider's own type, or not at all.
    sends          operation ids whose request body the provider sends as the
                   generated type; every other request body goes.
    [keep]         component = the properties the provider reads; the rest go,
                   with what only they referenced. For a oneOf or anyOf, the
                   components whose variants stay.
    strip          keywords dropped everywhere they are not an object; ones that
                   change no decoded shape and that upstream allOf parts
                   disagree on, which oapi-codegen refuses to merge. Without
                   format a timestamp stays the exact string the vendor sent.
    drop-examples  true drops media-type examples and the components only they use.
    [go-names]     "kind/name" (kind defaults to schemas) = the Go name for a
                   kept component whose generated name collides with an inline
                   enum or alias elsewhere in the slice.
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


def path_parameters(spec, item, operation):
    """The route's path parameters as plain strings: oapi-codegen requires each
    to be declared, and the provider builds the path itself."""

    found = []
    for parameter in (*item.get("parameters", ()), *operation.get("parameters", ())):
        if "$ref" in parameter:
            parameter = component(spec, parameter["$ref"])[2]
        if parameter.get("in") == "path":
            found.append({"name": parameter["name"], "in": "path", "required": True, "schema": {"type": "string"}})
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


def slice_operation(spec, item, operation, decodes, sends, roots):
    """Keep an operation's route contract and validate its decoded models."""

    op_id = operation["operationId"]
    answers = success(operation)
    kept = {
        "operationId": op_id,
        "parameters": path_parameters(spec, item, operation),
        "responses": {code: {"description": answer.get("description", "")} for code, answer in answers.items()},
    }
    upstream = reachable(spec, answers)
    for name in decodes.get(op_id, ()):
        if SCHEMAS + name not in upstream:
            raise SystemExit(f"{op_id} answers no {name}")
        roots.add(SCHEMAS + name)
    if op_id in sends:
        if "requestBody" not in operation:
            raise SystemExit(f"{op_id} sends no request body")
        kept["requestBody"] = copy.deepcopy(operation["requestBody"])
    return kept


def slice_paths(spec, wanted, settings):
    """Select declared operations and collect their decoded schema roots."""

    decodes = settings.get("decodes", {})
    sends = set(settings.get("sends", ()))
    if stray := (decodes.keys() | sends) - wanted:
        raise SystemExit(f"slice.toml names operations oapi-codegen.yaml does not: {sorted(stray)}")
    roots = set()
    paths = {}
    for route, item in spec["paths"].items():
        for method, operation in item.items():
            if method not in METHODS or operation.get("operationId") not in wanted:
                continue
            wanted.discard(operation["operationId"])
            paths.setdefault(route, {})[method] = slice_operation(spec, item, operation, decodes, sends, roots)
    if wanted:
        raise SystemExit(f"operations missing upstream: {sorted(wanted)}")
    if settings.get("drop-examples"):
        drop_media_examples(paths)
    return paths, roots


def slice_components(spec, paths, roots, settings):
    """Follow retained schema references after applying property narrowing."""

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
    with open(path, encoding="utf-8") as handle:
        spec = json.load(handle)
    if not spec.get("servers"):
        raise SystemExit("upstream names no servers")
    wanted = set(operation_ids(vendor / "oapi-codegen.yaml"))
    paths, roots = slice_paths(spec, wanted, settings)
    components = slice_components(spec, paths, roots, settings)
    name_components(components, settings.get("go-names", {}))
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

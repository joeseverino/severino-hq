"""Cut the Cloudflare operations the controller calls out of the upstream spec.

Upstream (github.com/cloudflare/api-schemas openapi.json) is 27 MB; the slice
keeps the operations in OPERATION_IDS and every component they reference,
transitively. Run against the pinned upstream file recorded in UPSTREAM:

    python3 slice.py /path/to/openapi.json > openapi.slice.json
"""

from __future__ import annotations

import json
import sys

# Every Cloudflare operation controller/providers/cloudflare*.go calls.
OPERATION_IDS = (
    "accounts-list-accounts",
    "access-applications-list-access-applications",
    "access-service-tokens-list-service-tokens",
    "certificate-packs-list-certificate-packs",
    "cloudflare-tunnel-configuration-get-configuration",
    "cloudflare-tunnel-list-cloudflare-tunnel-connections",
    "cloudflare-tunnel-list-cloudflare-tunnels",
    "d1-get-database",
    "d1-list-databases",
    "dns-records-for-a-zone-create-dns-record",
    "dns-records-for-a-zone-delete-dns-record",
    "dns-records-for-a-zone-list-dns-records",
    "dns-records-for-a-zone-update-dns-record",
    "getZoneRuleset",
    "listZoneRulesets",
    "page-rules-list-page-rules",
    "pages-project-get-projects",
    "registrar-domain-registration-list",
    "user-api-tokens-verify-token",
    "web-analytics-list-sites",
    "zone-settings-get-single-setting",
    "zones-get",
)

# Upstream schemas whose generated Go names collide with an inline enum or
# alias elsewhere in the slice; oapi-codegen needs x-go-name to tell them apart.
GO_NAMES = {
    "rulesets_Ruleset": "RulesetsRulesetSchema",
    "zones_automatic_https_rewrites_value": "ZonesAutomaticHTTPSRewritesValueSchema",
    "zones_browser_check_value": "ZonesBrowserCheckValueSchema",
    "zones_cache_level_value": "ZonesCacheLevelValueSchema",
    "zones_email_obfuscation_value": "ZonesEmailObfuscationValueSchema",
    "zones_ip_geolocation_value": "ZonesIPGeolocationValueSchema",
    "zones_mirage_value": "ZonesMirageValueSchema",
    "zones_opportunistic_encryption_value": "ZonesOpportunisticEncryptionValueSchema",
    "zones_origin_error_page_pass_thru_value": "ZonesOriginErrorPagePassThruValueSchema",
    "zones_polish_value": "ZonesPolishValueSchema",
    "zones_response_buffering_value": "ZonesResponseBufferingValueSchema",
    "zones_rocket_loader_value": "ZonesRocketLoaderValueSchema",
    "zones_security_level_value": "ZonesSecurityLevelValueSchema",
    "zones_sort_query_string_for_cache_value": "ZonesSortQueryStringForCacheValueSchema",
    "zones_ssl_value": "ZonesSslValueSchema",
    "zones_true_client_ip_header_value": "ZonesTrueClientIPHeaderValueSchema",
    "zones_waf_value": "ZonesWafValueSchema",
}

METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")


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


# Keywords that change no decoded shape; upstream allOf parts disagree on them,
# which oapi-codegen refuses to merge.
HINTS = ("readOnly", "writeOnly", "format", "default")


def strip_codegen_hints(node):
    """Drop HINTS. Without format a timestamp also stays the exact string
    Cloudflare sent, as the Python controller passes it through."""

    if isinstance(node, dict):
        for key in HINTS:
            if not isinstance(node.get(key), dict):
                node.pop(key, None)
        for value in node.values():
            strip_codegen_hints(value)
    elif isinstance(node, list):
        for value in node:
            strip_codegen_hints(value)


def component(spec, ref):
    _, _, kind, name = ref.split("/", 3)
    return kind, name, spec["components"][kind][name]


def main(path):
    with open(path, encoding="utf-8") as handle:
        spec = json.load(handle)
    wanted = set(OPERATION_IDS)
    paths = {}
    for route, item in spec["paths"].items():
        kept = {method: op for method, op in item.items() if method in METHODS and op.get("operationId") in wanted}
        if kept:
            shared = {key: value for key, value in item.items() if key not in METHODS}
            paths[route] = {**shared, **kept}
            wanted -= {op["operationId"] for op in kept.values()}
    if wanted:
        raise SystemExit(f"operations missing upstream: {sorted(wanted)}")
    pending, seen = set(), set()
    refs(paths, pending)
    components = {}
    while pending:
        ref = pending.pop()
        if ref in seen:
            continue
        seen.add(ref)
        kind, name, body = component(spec, ref)
        components.setdefault(kind, {})[name] = body
        refs(body, pending)
    for name, go_name in GO_NAMES.items():
        components["schemas"][name] = {**components["schemas"][name], "x-go-name": go_name}
    for kind in components:
        components[kind] = dict(sorted(components[kind].items()))
    strip_codegen_hints(paths)
    strip_codegen_hints(components)
    slice_spec = {
        "openapi": spec["openapi"],
        "info": {"title": spec["info"]["title"], "version": spec["info"]["version"]},
        "paths": dict(sorted(paths.items())),
        "components": dict(sorted(components.items())),
    }
    json.dump(slice_spec, sys.stdout, indent=1, sort_keys=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main(sys.argv[1])

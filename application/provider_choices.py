"""Answers that are a matter of live data rather than of type.

Most spec fields are fully described by their annotation: a port is an integer
between 1 and 65535, a scheme is one of two strings. A topology reference is
not. It has to name something that exists, and rendering it from the annotation
alone produced a blank text box that worked only if the operator already knew
the exact slug -- which meant the certificate form could be filled in correctly
only by someone who did not need it.

Kept out of ``control_plane.providers`` because these read the database and that
module declares rather than queries. Providers point at these by name, the same
late-bound way a domain points at its attention provider.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from control_plane.models import ManagedResource, ProviderInventory, TopologySnapshot
from control_plane.providers import DNS_RECORD_TYPES


def proxy_choices() -> dict[str, tuple[tuple[str, str], ...]]:
    """Certificates HQ manages, for a proxy host that needs one bound.

    ``certificate_resource`` names an HQ key, so typing it correctly required
    knowing what HQ had called something on another page. Blank stays first and
    means "keep whichever certificate the proxy already uses", which is what the
    reconciler does with an empty value.
    """

    managed = ManagedResource.objects.filter(
        kind="tls.certificate", enabled=True
    ).order_by("key")
    # No blank option here. Whether "leave it as it is" is even a coherent
    # answer depends on whether the thing exists yet, and only the form knows
    # that -- offered on a create page it read as "keep the certificate it
    # already has" about a proxy host that did not exist.
    return {
        "certificate_resource": tuple(
            (resource.key, resource.key) for resource in managed
        )
    }


def certificate_choices() -> dict[str, tuple[tuple[str, str], ...]]:
    """The certificates the topology describes, as ``pki:`` references.

    Labelled with the names each one covers, because "pki:jseverino-wildcard"
    does not tell an operator whether it answers for the host they are about to
    put behind it -- and that is the only question being asked at this point.
    """

    payload = (
        TopologySnapshot.objects.filter(pk="topology")
        .values_list("payload", flat=True)
        .first()
    )
    payload = payload or {}
    return {
        "topology_ref": (
            ("", "Define a new certificate below"),
            *_certificates(payload),
        ),
        "install_on": tuple(_install_targets(payload)),
    }


def _install_targets(payload: dict[str, Any]):
    """Places a certificate can be installed, learned from where they already are.

    Offered as the targets themselves rather than as deployment settings: an
    operator picks "the edge Caddy", and the directory it wants a certificate in
    is read back from how it already receives one.
    """

    seen: dict[str, str] = {}
    for dependency in payload.get("dependencies", ()):
        if dependency.get("relation") != "consumes":
            continue
        attributes = dependency.get("attributes") or {}
        connection_ref = attributes.get("connection_ref")
        kind = attributes.get("kind")
        if not connection_ref or not kind or connection_ref in seen:
            continue
        seen[connection_ref] = f"{connection_ref} ({kind})"
    return sorted(seen.items())


def _certificates(payload: dict[str, Any]):
    """Only entries a controller could actually issue and deploy.

    Not every pki entry is a certificate HQ can manage. An offline root CA is a
    signing key held on a machine with no network -- it has no domain list and
    nothing consumes it, and ``_resolve_tls`` refuses it for exactly those
    reasons. Offered in the menu it looked like a valid answer that failed only
    after being chosen, which is the worst place to find out.
    """

    consumed = {
        dependency.get("to")
        for dependency in payload.get("dependencies", ())
        if dependency.get("relation") == "consumes"
    }
    for entry in payload.get("pki", ()):
        identifier = entry.get("id")
        domains = entry.get("domains") or ()
        if not identifier or not domains:
            continue
        if f"pki:{identifier}" not in consumed:
            # Nothing declares that it serves this, so there is nowhere to
            # install it. Resolution treats that as an error, not an empty list.
            continue
        covers = ", ".join(domains[:4])
        if len(domains) > 4:
            covers += f", +{len(domains) - 4} more"
        yield (
            f"pki:{identifier}",
            f"{entry.get('certificate_name') or identifier} — {covers}",
        )


def uploaded_certificate_choices() -> dict[str, tuple[tuple[str, str], ...]]:
    """The same install targets an issued certificate can go to.

    Deploying is deploying: a proxy does not care which authority signed the
    thing it is asked to serve, so the targets are read back from use exactly
    the same way.
    """

    payload = (
        TopologySnapshot.objects.filter(pk="topology")
        .values_list("payload", flat=True)
        .first()
    )
    # Shared hosting is excluded: cPanel will not accept a certificate signed by
    # a private CA, and resolution refuses one. Offering it would put an answer
    # in the menu that fails only after being chosen.
    return {
        "install_on": tuple(
            (ref, label)
            for ref, label in _install_targets(payload or {})
            if "(cpanel)" not in label
        )
    }


def dns_record() -> dict[str, tuple[tuple[str, str], ...]]:
    """The zones a record could belong to, declared ones first.

    Not restricted to declared domains, deliberately. Restricting it reads as
    the stricter, safer choice and is neither: adopting a record from a zone
    that has not been declared yet produces a resource whose own zone is missing
    from the menu, so the next person to open its edit form is told their
    unmodified record is invalid. A menu that cannot describe what already
    exists is a worse failure than one that offers a zone nobody declared.

    Undeclared zones are labelled rather than hidden, which is the nudge without
    the dead end.
    """

    declared = {
        resource.spec.get("zone", "")
        for resource in ManagedResource.objects.filter(
            kind="cloudflare.zone", enabled=True
        )
        if resource.spec.get("zone")
    }
    seen = {
        str(record.get("zone", ""))
        for snapshot in ProviderInventory.objects.filter(kind="cloudflare.zone")
        for record in snapshot.records
        if record.get("zone")
    }
    options = [(zone, zone) for zone in sorted(declared)]
    options.extend(
        (zone, f"{zone} — not managed by HQ yet")
        for zone in sorted(seen - declared)
    )
    return {
        "zone": tuple(options),
        # Named rather than lettered. Rendered from the annotation alone the
        # menu reads "A / AAAA / CNAME / TXT / MX / CAA", which is a quiz for
        # anyone who does not already know the answer -- and the registry
        # already carries a sentence about each one.
        "record_type": tuple(
            (record_type.id, record_type.label) for record_type in DNS_RECORD_TYPES
        ),
    }


def zone() -> dict[str, tuple[tuple[str, str], ...]]:
    """The provider connections that could hold a zone.

    Read from the controller connection registry rather than from a list kept
    here, so adding a second Cloudflare account is a registry entry and not a
    code change. The registry is the same file the controller resolves
    credentials from, so a connection offered here is one that actually exists.
    """

    return {"connection_ref": tuple(_dns_connections())}


def _dns_connections():
    registry_path = (
        Path(__file__).resolve().parents[1] / "config" / "controller-connections.json"
    )
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # The form is still usable without the menu; refusing to render the page
        # because a config file is unreadable would be a worse failure than
        # asking the operator to type the reference.
        return
    for ref, connection in sorted((registry.get("connections") or {}).items()):
        provider = connection.get("provider", "")
        if not provider.startswith("cloudflare"):
            continue
        yield (ref, f"{ref} ({provider})")

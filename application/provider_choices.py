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

from typing import Any

from control_plane.models import ManagedResource, TopologySnapshot


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

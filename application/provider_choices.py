"""Answers that are a matter of live data rather than of type.

Most spec fields are fully described by their annotation: a port is an integer
between 1 and 65535, a scheme is one of two strings. Where a certificate
installs is not. It has to name something that exists, and rendering it from the
annotation alone produced a blank text box that worked only if the operator
already knew the exact slug -- which meant the certificate form could be filled
in correctly only by someone who did not need it.

Kept out of ``control_plane.providers`` because these read the database and that
module declares rather than queries. Providers point at these by name, the same
late-bound way a domain points at its attention provider.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet

from control_plane.models import ManagedResource, ProviderInventory
from control_plane.providers import (
    CERTIFICATE_KIND,
    DELIVERY_TARGET_KIND,
    DNS_RECORD_TYPES,
    MACHINE_KIND,
    NameContext,
)

from .connections import connections_for, reachable_through


def proxy_choices(context: NameContext) -> dict[str, tuple[tuple[str, str], ...]]:
    """Certificates HQ manages, for a proxy host that needs one bound.

    ``certificate_resource`` names an HQ key, so typing it correctly required
    knowing what HQ had called something on another page. Blank stays first and
    means "keep whichever certificate the proxy already uses", which is what the
    reconciler does with an empty value.
    """

    managed = ManagedResource.objects.filter(
        kind=CERTIFICATE_KIND, enabled=True
    ).order_by("key")
    covering = set(context.certificates)
    # The ones that answer for this name first, and marked. With a single
    # certificate the menu was right by accident; the second one is a coin
    # flip, and binding a proxy to a certificate that does not cover its names
    # is a browser warning rather than an error anything reports.
    options = [
        (resource.key, f"{resource.key} — covers {context.hostname}")
        for resource in managed
        if resource.key in covering
    ]
    options.extend(
        (resource.key, resource.key)
        for resource in managed
        if resource.key not in covering
    )
    # No blank option here. Whether "leave it as it is" is even a coherent
    # answer depends on whether the thing exists yet, and only the form knows
    # that -- offered on a create page it read as "keep the certificate it
    # already has" about a proxy host that did not exist.
    return {"certificate_resource": tuple(options)}


def certificate_choices(context: NameContext) -> dict[str, tuple[tuple[str, str], ...]]:
    """Where a certificate can be installed."""

    return {"install_on": tuple(_install_targets())}


def _install_targets(exclude: AbstractSet[str] = frozenset()):
    """Every declared delivery target, as the thing an operator picks.

    Offered as the targets themselves rather than as deployment settings: an
    operator picks "the edge Caddy", and the directory it wants a certificate in
    is stated once on the target.
    """

    targets = ManagedResource.objects.filter(
        kind=DELIVERY_TARGET_KIND, enabled=True
    ).values_list("spec", flat=True)
    return sorted(
        (spec["connection_ref"], f"{spec['name']} ({spec['kind']})")
        for spec in targets
        if spec.get("connection_ref") and spec.get("kind") not in exclude
    )


def delivery_target(context: NameContext) -> dict[str, tuple[tuple[str, str], ...]]:
    """Which credential reaches the target, and which certificate names it."""

    return {
        "connection_ref": _connection_choices("npm") + _connection_choices("ssh"),
        "certificate_resource": (
            ("", "Nothing yet"),
            *sorted(
                (key, key)
                for key in ManagedResource.objects.filter(
                    kind=CERTIFICATE_KIND, enabled=True
                ).values_list("key", flat=True)
            ),
        ),
    }


def uploaded_certificate_choices(context: NameContext) -> dict[str, tuple[tuple[str, str], ...]]:
    """The same install targets an issued certificate can go to, less cPanel.

    Deploying is deploying: a proxy does not care which authority signed the
    thing it is asked to serve. Shared hosting does -- cPanel will not accept a
    certificate signed by a private CA, and resolution refuses one, so offering
    it would put an answer in the menu that fails only after being chosen.
    """

    return {"install_on": tuple(_install_targets(exclude={"cpanel"}))}


def dns_record(context: NameContext) -> dict[str, tuple[tuple[str, str], ...]]:
    """The zone a record belongs to, and what kind of record it is."""

    return {
        "zone": _known_zones(),
        # Named rather than lettered. Rendered from the annotation alone the
        # menu reads "A / AAAA / CNAME / TXT / MX / CAA", which is a quiz for
        # anyone who does not already know the answer -- and the registry
        # already carries a sentence about each one.
        "record_type": tuple(
            (record_type.id, record_type.label) for record_type in DNS_RECORD_TYPES
        ),
    }


def container_stack(context: NameContext) -> dict[str, tuple[tuple[str, str], ...]]:
    """Where a stack can run, and which Portainer reaches it.

    Both menus come from the connection sweep, because a Portainer is the only
    thing that knows which machines it holds -- HQ lists a printer and a phone as
    readily as a Docker host, and a machine registered this morning is available
    whether or not anything has been declared about it.

    Read from the connection rather than from the containers found on it, so a
    machine that is running nothing is still offered. That is precisely when
    this form is being filled in.
    """

    described = _declared_roles()
    return {
        # Labelled with what the machine is for where HQ has been told, because
        # Portainer calls its own host "local" and that is nobody's hostname.
        "host": tuple(
            (host, f"{host} — {described[host]}" if described.get(host) else host)
            for host, _ in reachable_through("portainer")
        ),
        "connection_ref": _connection_choices("portainer"),
    }


def _declared_roles() -> dict[str, str]:
    """What each declared machine is for, for labelling only."""

    return {
        spec["name"]: spec.get("role", "")
        for spec in ManagedResource.objects.filter(
            kind=MACHINE_KIND, enabled=True
        ).values_list("spec", flat=True)
        if spec.get("name")
    }


def zone(context: NameContext) -> dict[str, tuple[tuple[str, str], ...]]:
    """Which domain to take responsibility for, and through which credential."""

    return {
        "zone": _known_zones(),
        "connection_ref": _connection_choices("cloudflare_dns"),
    }


def _known_zones() -> tuple[tuple[str, str], ...]:
    """Every zone HQ has seen, declared ones first.

    Not restricted to declared domains, deliberately. Restricting it reads as
    the stricter, safer choice and is neither: adopting a record from a zone
    that has not been declared yet produces a resource whose own zone is missing
    from the menu, so the next person to open its edit form is told their
    unmodified record is invalid. A menu that cannot describe what already
    exists is a worse failure than one that offers a zone nobody declared.

    Undeclared zones are labelled rather than hidden, which is the nudge without
    the dead end.

    Shared by the two forms that ask which domain, so declaring one and adding a
    record to one cannot come to disagree about what a domain is.
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
    return tuple(options)


def _connection_choices(provider: str) -> tuple[tuple[str, str], ...]:
    """The connections of one kind, as the controller last reported them.

    Empty until the first sweep, and the field stays typeable -- an empty menu
    is a smaller failure than a menu that cannot describe what already exists.
    A connection that stopped answering is still offered, and says so: it is
    the one an operator already has, and hiding it reads as never having set
    it up.
    """

    return tuple(
        (
            connection.connection_ref,
            connection.connection_ref
            if connection.reachable
            else f"{connection.connection_ref} (not answering)",
        )
        for connection in connections_for(provider)
    )

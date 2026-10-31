"""Answers that are a matter of live data rather than of type.

Most spec fields are described by their annotation; where a certificate
installs has to name something that exists, so it is a menu of what exists
rather than a text box that needs the exact slug. Kept out of
``control_plane.providers``, which declares rather than queries; providers name
these late-bound, as a domain names its attention provider.
"""

from collections.abc import Set as AbstractSet

from hq.domains.control_plane.models import ManagedResource, ProviderInventory
from hq.domains.control_plane.provider_adapters.cloudflare import (
    DNS_RECORD_TYPES,
    ZONE_KIND,
)
from hq.domains.control_plane.provider_adapters.declarations import (
    DELIVERY_TARGET_KIND,
    MACHINE_KIND,
)
from hq.domains.control_plane.provider_adapters.portainer import CONTAINER_KIND
from hq.domains.control_plane.provider_adapters.tailscale import TAILNET_KIND
from hq.domains.control_plane.provider_adapters.tls import (
    CERTIFICATE_KIND,
    UPLOADED_CERTIFICATE_KIND,
    UPLOADED_CERTIFICATE_REFUSALS,
)
from hq.domains.control_plane.provider_spec import NameContext
from hq.domains.control_plane.providers import PROVIDERS

from .connections import connections_for, reachable_through
lazy from .tailnet_presence import observed_addresses


def proxy_choices(context: NameContext) -> dict[str, tuple[tuple[str, str], ...]]:
    """Certificates HQ manages, for a proxy host that needs one bound.

    ``certificate_resource`` names an HQ key, which an operator should pick
    rather than type from memory of another page. Blank stays first and
    means "keep whichever certificate the proxy already uses", which is what the
    reconciler does with an empty value.
    """

    # Both kinds. A name no public authority will issue for is served by a
    # certificate HQ was given rather than one it issued, so an uploaded
    # certificate must be offered for the proxy that needs it.
    managed = ManagedResource.objects.filter(
        kind__in=(CERTIFICATE_KIND, UPLOADED_CERTIFICATE_KIND), enabled=True
    ).order_by("key")
    covering = set(context.certificates)
    # The ones that answer for this name first, and marked: binding a proxy
    # to a certificate that does not cover its names is a browser warning
    # rather than an error anything reports.
    options = [
        (resource.key, f"{resource.key} · covers {context.hostname}")
        for resource in managed
        if resource.key in covering
    ]
    options.extend((resource.key, resource.key) for resource in managed if resource.key not in covering)
    # No blank option here. Whether "leave it as it is" is even a coherent
    # answer depends on whether the thing exists yet, and only the form knows
    # that: on a create page there is no existing certificate to keep.
    return {"certificate_resource": tuple(options)}


def certificate_choices(_context: NameContext) -> dict[str, tuple[tuple[str, str], ...]]:
    """Where a certificate can be installed."""

    return {"install_on": tuple(_install_targets())}


def _install_targets(exclude: AbstractSet[str] = frozenset()):
    """Every declared delivery target, as the thing an operator picks.

    Offered as the targets themselves rather than as deployment settings: an
    operator picks "the edge Caddy", and the directory it wants a certificate in
    is stated once on the target.
    """

    targets = ManagedResource.objects.filter(kind=DELIVERY_TARGET_KIND, enabled=True).values_list("spec", flat=True)
    return sorted(
        (spec["connection_ref"], f"{spec['name']} ({spec['kind']})")
        for spec in targets
        if spec.get("connection_ref") and spec.get("kind") not in exclude
    )


def tailnet_device(_context: NameContext) -> dict[str, tuple[tuple[str, str], ...]]:
    """The devices the tailnet actually reported, and the credential to use.

    Offered rather than typed: the name has to match what the tailnet calls the
    device exactly, and that is rarely what anyone would guess: a laptop is
    whatever its owner typed into it years ago.
    """

    devices: dict[str, str] = {}
    for snapshot in ProviderInventory.objects.filter(kind=TAILNET_KIND):
        for record in snapshot.records:
            name = str(record.get("name", ""))
            if not name:
                continue
            devices[name] = (
                f"{name} · key expires {record['key_expires'][:10]}"
                if record.get("key_expires")
                else f"{name} · key expiry disabled"
            )
    return {
        "name": tuple(sorted(devices.items())),
        "connection_ref": _connections_of(TAILNET_KIND),
    }


def delivery_target(_context: NameContext) -> dict[str, tuple[tuple[str, str], ...]]:
    """Which credential reaches the target, and which certificate names it."""

    return {
        "connection_ref": _connections_of(DELIVERY_TARGET_KIND),
        "certificate_resource": (
            ("", "None"),
            *sorted(
                (key, key)
                for key in ManagedResource.objects.filter(kind=CERTIFICATE_KIND, enabled=True).values_list(
                    "key", flat=True
                )
            ),
        ),
    }


def uploaded_certificate_choices(_context: NameContext) -> dict[str, tuple[tuple[str, str], ...]]:
    """The same install targets an issued certificate can go to, less cPanel.

    Deploying is deploying: a proxy does not care which authority signed the
    thing it is asked to serve. Shared hosting does: cPanel will not accept a
    certificate signed by a private CA, and resolution refuses one, so offering
    it would put an answer in the menu that fails only after being chosen.
    """

    # The targets resolution refuses an uploaded certificate are not offered.
    return {"install_on": tuple(_install_targets(exclude=UPLOADED_CERTIFICATE_REFUSALS.keys()))}


def dns_record(_context: NameContext) -> dict[str, tuple[tuple[str, str], ...]]:
    """The zone a record belongs to, and what kind of record it is."""

    return {
        "zone": _known_zones(),
        # Named rather than lettered. Rendered from the annotation alone the
        # menu reads "A / AAAA / CNAME / TXT / MX / CAA", which is a quiz for
        # anyone who does not already know the answer, and the registry
        # already carries a sentence about each one.
        "record_type": tuple((record_type.id, record_type.label) for record_type in DNS_RECORD_TYPES),
    }


def container_stack(_context: NameContext) -> dict[str, tuple[tuple[str, str], ...]]:
    """Where a stack can run, and which Portainer reaches it.

    Both menus come from the connection sweep, since only a Portainer knows
    which machines it holds: HQ lists a printer as readily as a Docker host, and
    a machine registered this morning is available whether or not anything is
    declared about it. Read from the connection rather than its containers, so a
    machine running nothing, exactly when this form is filled in, is offered.
    """

    described = _declared_roles()
    return {
        # Labeled with what the machine is for where HQ has been told, because
        # Portainer calls its own host "local" and that is nobody's hostname.
        "host": tuple(
            (host, f"{host} · {described[host]}" if described.get(host) else host)
            for provider in PROVIDERS[CONTAINER_KIND].connection_providers
            for host, _ in reachable_through(provider)
        ),
        "connection_ref": _connections_of(CONTAINER_KIND),
    }


def _declared_roles() -> dict[str, str]:
    """What each declared machine is for, for labeling only."""

    return {
        spec["name"]: spec.get("role", "")
        for spec in ManagedResource.objects.filter(kind=MACHINE_KIND, enabled=True).values_list("spec", flat=True)
        if spec.get("name")
    }


def zone(_context: NameContext) -> dict[str, tuple[tuple[str, str], ...]]:
    """Which domain to take responsibility for, and through which credential."""

    return {
        "zone": _known_zones(),
        "connection_ref": _connections_of(ZONE_KIND),
    }


def _known_zones() -> tuple[tuple[str, str], ...]:
    """Every zone HQ has seen, declared ones first and undeclared ones labeled.

    Restricting it to declared domains reads as safer and is not: a record
    adopted from an undeclared zone would find its own zone missing from the
    menu, and its next editor would be told the unmodified record is invalid.
    Labeled rather than hidden is the nudge without the dead end. Shared by both
    forms that ask which domain, so they cannot disagree about what one is.
    """

    declared = {
        resource.spec.get("zone", "")
        for resource in ManagedResource.objects.filter(kind=ZONE_KIND, enabled=True)
        if resource.spec.get("zone")
    }
    seen = {
        str(record.get("zone", ""))
        for snapshot in ProviderInventory.objects.filter(kind=ZONE_KIND)
        for record in snapshot.records
        if record.get("zone")
    }
    options = [(zone, zone) for zone in sorted(declared)]
    options.extend((zone, f"{zone} · not managed") for zone in sorted(seen - declared))
    return tuple(options)


def _connections_of(kind: str) -> tuple[tuple[str, str], ...]:
    """The connections of every provider the registry says reaches ``kind``."""

    return tuple(
        choice for provider in PROVIDERS[kind].connection_providers for choice in _connection_choices(provider)
    )


def _connection_choices(provider: str) -> tuple[tuple[str, str], ...]:
    """The connections of one kind, as the controller last reported them.

    Empty until the first sweep, and the field stays typeable: an empty menu
    is a smaller failure than a menu that cannot describe what already exists.
    A connection that stopped answering is still offered, and says so: it is
    the one an operator already has, and hiding it reads as never having set
    it up.
    """

    return tuple(
        (
            connection.connection_ref,
            connection.connection_ref if connection.reachable else f"{connection.connection_ref} (unreachable)",
        )
        for connection in connections_for(provider)
    )


def machine_address_notes() -> dict[str, dict[str, str]]:
    """Which of a machine's addresses HQ can see without being told.

    Some declared addresses are the only record there is (a LAN printer, a VPS's
    public address); the rest repeat the tailnet's reading on every sweep and
    tie HQ's name for a machine to the tailnet's. The field stays writable and
    says which is which: locking it would break the printer, and silence invites
    somebody to correct HQ about a value HQ is watching.
    """

    return {"addresses": observed_addresses()}

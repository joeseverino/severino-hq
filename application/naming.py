"""What HQ already knows about a hostname, gathered once for whatever asks next.

Every fact here is the output of work done elsewhere: the connection sweep says
which zones a credential may edit, the service view resolves where a name is
already served, the certificate registry knows what already covers it. Each was
reachable and none was reaching the place it was needed, so a form asked for the
address printed on the card above it, and offered to issue a certificate for a
name no credential could prove.

Kept out of ``control_plane.providers`` because this queries and that module
declares. Providers say what they would do with each fact; this is the half that
goes and gets them.
"""

from __future__ import annotations

from control_plane.models import ManagedResource, ProviderConnection, TopologySnapshot
from control_plane.providers import PROVIDERS, NameContext, certificate_covers

from .infrastructure import resolved_spec


def name_context(hostname: str) -> NameContext:
    """Everything HQ can say about one name, without being told.

    Built per request rather than cached. It is three small queries, and a cache
    would answer "which zones can you reach" with what was true before the
    credential was replaced -- which is the one question whose staleness leads
    somewhere expensive.
    """

    hostname = hostname.strip().lower().rstrip(".")
    if not hostname:
        return NameContext()
    zones, swept = _reported_zones()
    return NameContext(
        hostname=hostname,
        public_zones=zones,
        swept=swept,
        origin=(origin := _origin_for(hostname)),
        origin_address=_reachable(origin),
        certificates=_covering(hostname),
    )


def _reported_zones() -> tuple[tuple[str, ...], bool]:
    """Zones a connected credential last said it could edit, and whether asked.

    The second half matters as much as the first. Nothing having swept and
    nothing being reachable produce the same empty tuple, and a provider that
    read them the same way would refuse every public name on a fresh install.
    """

    # Which connections hold public zones is derived from the providers that
    # say their effect is public. Named directly, this file would carry the one
    # word -- "cloudflare_dns" -- that the rest of the pass exists to remove.
    public = {
        provider
        for spec in PROVIDERS.values()
        if spec.public_effect
        for provider in spec.connection_providers
    }
    # Only a connection that answered. One that exists and failed its probe
    # reports no zones, and counted as having reported it would turn an expired
    # token into "no connected account holds a zone for jseverino.com" -- HQ
    # refusing to publish a record in a domain it owns, on the strength of not
    # having been able to ask.
    reported = [
        connection
        for connection in ProviderConnection.objects.all()
        if connection.provider in public and connection.reachable and connection.probed
    ]
    zones = {
        str(zone).strip().lower().rstrip(".")
        for connection in reported
        for zone in connection.reaches
        if zone
    }
    return tuple(sorted(zones)), bool(reported)


def _origin_for(hostname: str) -> str:
    """Where this name is already served, as ``host:port``.

    Read through the providers' own ``origin`` hooks rather than by reaching
    into any spec, so a provider that starts answering the question joins this
    by declaring it -- the same way it joins the service view.
    """

    topology = _topology()
    for resource in ManagedResource.objects.filter(enabled=True):
        provider = PROVIDERS.get(resource.kind)
        if provider is None or provider.origin is None or provider.hostnames is None:
            continue
        spec = resolved_spec(resource, topology)
        try:
            names = {
                str(name).strip().lower().rstrip(".")
                for name in provider.hostnames(spec)
            }
            if hostname not in names:
                continue
            origin = provider.origin(spec)
        except (KeyError, TypeError, ValueError):
            continue
        if origin:
            return origin
    return ""


def _reachable(origin: str) -> str:
    """The same origin, with the machine named as the network reaches it.

    A stack says it runs on ``homelab-server`` because that is what the machine
    is called, and everything inside HQ matches on it. A proxy is not inside HQ:
    nginx resolves what it is given, and it has never heard the name.

    Unchanged when the topology knows no address for the machine, which leaves
    the operator a wrong-looking value to correct rather than a plausible one to
    trust.
    """

    host, _, port = origin.rpartition(":")
    if not host or not port:
        return origin
    for machine in (_topology() or {}).get("hosts", ()):
        if machine.get("id") != host:
            continue
        address = machine.get("lan_ip") or machine.get("ts_ip") or ""
        return f"{address}:{port}" if address else origin
    return origin


def _topology() -> dict | None:
    return (
        TopologySnapshot.objects.filter(pk="topology")
        .values_list("payload", flat=True)
        .first()
    )


def _covering(hostname: str) -> tuple[str, ...]:
    """Certificates that already answer for this name, wildcards included.

    Matched by the same rule the service page uses, so a proxy is offered the
    certificate the page says covers it and the two cannot disagree.
    """

    topology = _topology()
    found = []
    for resource in ManagedResource.objects.filter(enabled=True):
        provider = PROVIDERS.get(resource.kind)
        if provider is None or not provider.covers or provider.hostnames is None:
            continue
        # Resolved, not authored. A certificate defined by a topology reference
        # carries no domains in its own spec, so the authored form answers "this
        # covers nothing" about the wildcard covering everything.
        try:
            names = frozenset(provider.hostnames(resolved_spec(resource, topology)))
        except (KeyError, TypeError, ValueError):
            continue
        if certificate_covers(hostname, names):
            found.append(resource.key)
    return tuple(sorted(found))

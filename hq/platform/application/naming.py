"""What HQ already knows about a hostname, gathered once for whatever asks next.

Every fact here is work done elsewhere (the sweep's editable zones, where a name
is already served, what certificate covers it), brought to where it is needed so
a form does not ask for an address HQ already shows, or offer a certificate for
a name no credential can prove. Kept out of ``control_plane.providers``, which
declares what it would do with each fact; this is the half that queries.
"""

from hq.domains.control_plane.names import certificate_covers, normalized_hostname
from hq.domains.control_plane.provider_spec import NameContext
from hq.domains.control_plane.providers import PROVIDERS

from .infrastructure import (
    declared_machines,
    delivery_targets,
    enabled_resources,
    resolved_spec,
)
from .locate import index_of, join_endpoint, split_endpoint
from .projection import projection_scope, read_once
lazy from .connections import connection_rows


def name_context(hostname: str) -> NameContext:
    """Everything HQ can say about one name, without being told.

    Built per request rather than cached: a cache that outlived the request
    would answer "which zones can you reach" with what was true before the
    credential was replaced, the one question whose staleness leads somewhere
    expensive.
    """

    hostname = normalized_hostname(hostname)
    if not hostname:
        return NameContext()
    # Within a request the reads are shared through `read_once`, so a page that
    # asks about many names resolves each resource once, not once per name and
    # reader. This joins the caller's scope when there is one; otherwise one of
    # its own, so even a single ask resolves each resource once.
    with projection_scope():
        zones, swept = _reported_zones()
        return NameContext(
            hostname=hostname,
            public_zones=zones,
            swept=swept,
            origin=(origin := _origin_for(hostname)),
            origin_address=_reachable(origin),
            certificates=_covering(hostname),
        )


def public_zones_declared() -> tuple[str, ...]:
    """Zones an enabled declaration of a public, record-holding provider manages."""

    def load() -> tuple[str, ...]:
        found = set()
        for resource in enabled_resources():
            provider = PROVIDERS.get(resource.kind)
            if provider is not None and provider.contains and provider.public_effect:
                zone = normalized_hostname(resource.spec.get("zone"))
                if zone:
                    found.add(zone)
        return tuple(sorted(found))

    return read_once("naming.public_zones_declared", load)


def _reported_zones() -> tuple[tuple[str, ...], bool]:
    return read_once("naming.reported_zones", _load_reported_zones)


def _enabled_resolved() -> tuple[tuple[object, object, object], ...]:
    """Every enabled resource with a provider, its spec resolved once.

    `(resource, provider, spec)`, where spec is the exception resolving raised
    when it did, so each reader keeps its own rule for a spec that will not
    resolve: `_origin_for` lets it surface, `_covering` passes over it.
    """

    def load():
        targets = delivery_targets()
        rows = []
        for resource in enabled_resources():
            provider = PROVIDERS.get(resource.kind)
            if provider is None or provider.hostnames is None:
                continue
            if provider.origin is None and not provider.covers:
                continue
            try:
                spec = resolved_spec(resource, targets)
            except Exception as exc:  # noqa: BLE001 - re-raised or skipped by the reader
                spec = exc
            rows.append((resource, provider, spec))
        return tuple(rows)

    return read_once("naming.enabled_resolved", load)


def _load_reported_zones() -> tuple[tuple[str, ...], bool]:
    """Zones a connected credential last said it could edit, and whether asked.

    The second half matters as much as the first. Nothing having swept and
    nothing being reachable produce the same empty tuple, and a provider that
    read them the same way would refuse every public name on a fresh install.
    """

    # Which connections hold public zones is derived from the providers that
    # say their effect is public, so this file names no provider.
    public = {provider for spec in PROVIDERS.values() if spec.public_effect for provider in spec.connection_providers}
    # Only a connection that answered. One that exists and failed its probe
    # reports no zones, and counted as having reported it would turn an expired
    # token into "no connected account holds a zone for example.com", and HQ
    # would refuse to publish in a domain it owns because it could not ask.
    reported = [
        connection
        for connection in _connection_rows()
        if connection.provider in public and connection.reachable and connection.probed
    ]
    zones = {normalized_hostname(zone) for connection in reported for zone in connection.reaches if zone}
    return tuple(sorted(zones)), bool(reported)


def _origin_for(hostname: str) -> str:
    """Where this name is already served, as ``host:port``.

    Read through the providers' own ``origin`` hooks rather than by reaching
    into any spec, so a provider that starts answering the question joins this
    by declaring it: the same way it joins the service view.
    """

    for _resource, provider, spec in _enabled_resolved():
        if provider.origin is None:
            continue
        if isinstance(spec, Exception):
            raise spec
        try:
            names = {normalized_hostname(name) for name in provider.hostnames(spec)}
            if hostname not in names:
                continue
            origin = provider.origin(spec)
        except KeyError, TypeError, ValueError:
            continue
        if origin:
            return origin
    return ""


def _reachable(origin: str) -> str:
    """The same origin, with the machine named as the network reaches it.

    A stack names its machine by name, which everything inside HQ matches on; a
    proxy is not HQ, and nginx has never heard the name. Unchanged when HQ knows
    no address for the machine, which leaves the operator a wrong-looking value
    to correct rather than a plausible one to trust.
    """

    host, port = split_endpoint(origin)
    if not host or not port:
        return origin
    # The inverse of what every other surface asks, of the same index, so the
    # two directions cannot disagree about which pair belongs together. From
    # declarations alone: this fills in a form field, whose address should be
    # the one somebody wrote down, not one inferred from where a credential
    # points; and it is one query on a path that runs once per service.
    index = read_once("naming.declared_index", lambda: index_of(declared=declared_machines()))
    address = index.address_for(host)
    return join_endpoint(address, port) if address else origin


def _covering(hostname: str) -> tuple[str, ...]:
    """Certificates that already answer for this name, wildcards included.

    Matched by the same rule the service page uses, so a proxy is offered the
    certificate the page says covers it and the two cannot disagree.
    """

    found = []
    for resource, provider, spec in _enabled_resolved():
        if not provider.covers:
            continue
        if isinstance(spec, (KeyError, TypeError, ValueError)):
            continue
        if isinstance(spec, Exception):
            raise spec
        # Resolved, not authored, so this reads the same names the service page
        # does: one rule for what a certificate covers, not two.
        try:
            names = frozenset(provider.hostnames(spec))
        except KeyError, TypeError, ValueError:
            continue
        if certificate_covers(hostname, names):
            found.append(resource.key)
    return tuple(sorted(found))


def _connection_rows() -> tuple:
    return connection_rows()

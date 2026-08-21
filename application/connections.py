"""What HQ can reach, and what each of those things can act on.

A connection is a credential, and HQ holds none. It holds the *report* of one:
the controller renders the vault into its own environment, asks each endpoint
whether it still answers and what it can see, and sends that back. So this
module reads a cache and never a secret, and the page it feeds is a view of the
vault that cannot drift from it -- there is no second list to keep in step.

The point of ``reaches`` is that it is the only place some facts exist at all.
Nothing in HQ can know which machines a Portainer holds or which zones a token
may edit; the credential that would have to carry out the work is the only thing
that can say. Every menu asking "which machine" or "which domain" is derived
from it, which is what makes adding a VPS a matter of registering it with
Portainer rather than of editing anything here.
"""

from __future__ import annotations

from dataclasses import dataclass

from control_plane.models import ProviderConnection
from control_plane.providers import PROVIDERS


@dataclass(frozen=True)
class ConnectionReading:
    """One connection, with what HQ would use it for."""

    connection_ref: str
    controller_id: str
    provider: str
    endpoint: str
    reaches: tuple[str, ...]
    reachable: bool
    probed: bool
    detail: str
    observed_at: object
    # The kinds of thing this connection is what makes possible. Derived from
    # the providers rather than stored, so a provider added tomorrow lists
    # itself against the connections that could serve it.
    supplies: tuple[str, ...]

    @property
    def status(self) -> str:
        if not self.reachable:
            return "unreachable"
        return "reachable" if self.probed else "unprobed"


def _supplies(provider: str) -> tuple[str, ...]:
    """Which resource kinds this sort of connection can stand behind."""

    if not provider:
        return ()
    return tuple(
        sorted(
            spec.label or kind
            for kind, spec in PROVIDERS.items()
            if provider in spec.connection_providers
        )
    )


def connection_readings() -> tuple[ConnectionReading, ...]:
    """Every connection every controller last reported."""

    return tuple(
        ConnectionReading(
            connection_ref=row.connection_ref,
            controller_id=row.controller_id,
            provider=row.provider,
            endpoint=row.endpoint,
            reaches=tuple(row.reaches),
            reachable=row.reachable,
            probed=row.probed,
            detail=row.detail,
            observed_at=row.observed_at,
            supplies=_supplies(row.provider),
        )
        for row in ProviderConnection.objects.all()
    )


def connections_for(provider: str) -> tuple[ProviderConnection, ...]:
    """The connections that are one of these, reachable ones first.

    Ordering is the whole contract: a menu built from this offers a working
    credential before a broken one, and never silently omits the broken one --
    an operator whose token expired needs to see the connection they already
    have, marked, rather than an empty list that reads as "you never set it up".
    """

    return tuple(
        sorted(
            ProviderConnection.objects.filter(provider=provider),
            key=lambda row: (not row.reachable, row.connection_ref),
        )
    )


def reachable_through(provider: str) -> tuple[tuple[str, str], ...]:
    """Everything the connections of one kind can act on, as (name, connection).

    A machine behind two Portainers is listed once, under the first that can
    reach it, because the question a form is asking is "where does this run",
    not "by which route".
    """

    seen: dict[str, str] = {}
    for connection in connections_for(provider):
        for name in connection.reaches:
            seen.setdefault(name, connection.connection_ref)
    return tuple(sorted(seen.items()))


def consoles() -> tuple[tuple[str, str, str], ...]:
    """Connections that are a thing you can open, as (label, sub, url).

    A console and an API base are both URLs and only one is worth a link. Told
    apart by the shape a credential's endpoint already has: an API is reached at
    a path -- a version, a prefix -- and a console is reached at the host
    itself. So this offers Portainer and the proxy and leaves out the DNS API,
    without a list here naming any of them.

    Nothing is hand-authored, which is the point: these used to be four URLs
    written into a public repository, one deployment's addresses shipped to
    everybody who cloned it.
    """

    from urllib.parse import urlsplit

    found = []
    for connection in ProviderConnection.objects.all():
        endpoint = connection.endpoint.strip()
        if not endpoint or "://" not in endpoint:
            continue
        parsed = urlsplit(endpoint)
        if parsed.path.strip("/"):
            continue
        found.append(
            (
                connection.provider.replace("_", " ").title()
                or connection.connection_ref,
                connection.connection_ref,
                endpoint,
            )
        )
    return tuple(sorted(found))


def operator_links() -> list[dict[str, str]]:
    """Extra dashboard links this deployment wants, from its own environment.

    A status page or a public site is a fact about one installation and belongs
    with its other deployment facts. Malformed input is ignored rather than
    fatal: a dashboard is where an operator goes to fix things, and refusing to
    render it over a bad link is the least useful moment to fail.
    """

    import json

    from django.conf import settings

    raw = str(getattr(settings, "SEVERINO_DASHBOARD_LINKS", "") or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    return [
        {
            "label": str(item.get("label", ""))[:80],
            "sub": str(item.get("sub", ""))[:80],
            "href": str(item.get("href", ""))[:500],
        }
        for item in parsed
        if isinstance(item, dict) and str(item.get("href", "")).startswith("http")
    ]

"""One facet of a service: what each kind of declaration says about a name, and the zone that holds it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from control_plane.models import ProviderInventory
from control_plane.names import in_zone
from control_plane.providers import PROVIDERS
from control_plane.provider_spec import NameContext

from .containers import Running
from .entity_links import EntityLink, entity_link, kind_label
from .facts import Joined
from .labels import lower_first
from .projection import read_once
from .service_declarations import Claim
from .ui import moment


@dataclass(frozen=True)
class ReadingLine:
    """The readings of one kind that supply a facet, as one line."""

    label: str
    detail: str = ""
    relation: str = ""
    # The earliest expiry among them, as a person reads it.
    expiry: str = ""
    # Whether ``detail`` names issuers rather than the records themselves.
    issued: bool = False
    # Each record, through the link builder.
    entities: tuple[EntityLink, ...] = ()
    stale: bool = False

    @property
    def hint(self) -> str:
        return f"{self.relation} · earliest expires {self.expiry}" if self.expiry else self.relation


@dataclass(frozen=True)
class Facet:
    """One thing that has to be true for a hostname to answer, and whether it is."""

    id: str
    label: str
    claims: tuple[Claim, ...] = ()
    # What HQ can see supplying this that no declaration accounts for. A facet
    # has three states, not two: declared, found, and absent. Collapsing the
    # middle one into absent reports a running service as missing, and offers to
    # build a second of what is already there.
    observed: "Running | None" = None
    # The machine whatever supplies this facet runs on. Held here so the card
    # links it once, whether the container is declared or merely observed.
    machine: Any = None
    # What HQ knows about this name. Held so ``declarable`` can ask each
    # provider whether it could actually supply it: an offer that cannot work
    # is worse than no offer, and only the provider knows which is which.
    context: NameContext = field(default_factory=NameContext)
    # Readings joined to the name that supply this facet: an edge certificate,
    # a tunnel. Observed, never a claim.
    readings: tuple[Joined, ...] = ()

    @property
    def present(self) -> bool:
        return bool(self.claims)

    @property
    def reading_lines(self) -> tuple["ReadingLine", ...]:
        """One line per reading kind: its short label, issuers or titles, earliest expiry."""

        by_kind: dict[str, list[Joined]] = {}
        for item in self.readings:
            by_kind.setdefault(item.kind, []).append(item)
        lines = []
        for items in by_kind.values():
            spec = items[0].spec
            issuers = tuple(dict.fromkeys(item.issuer for item in items if item.issuer))
            titles = tuple(dict.fromkeys(item.title for item in items if item.title))
            dated = [(when, item) for item in items if (when := moment(item.expires))]
            earliest = min(dated, key=lambda pair: pair[0])[1].expiry if dated else ""
            lines.append(
                ReadingLine(
                    label=spec.short,
                    detail=", ".join(issuers or titles),
                    relation=items[0].relation,
                    expiry=earliest,
                    issued=bool(issuers),
                    entities=tuple(
                        dict.fromkeys(
                            entity_link(item.kind, "", record=item.record) for item in items
                        )
                    ),
                    stale=any(item.stale for item in items),
                )
            )
        return tuple(lines)

    @property
    def not_visible(self) -> str:
        """Why nothing HQ reads could show this facet, or "" when something could.

        "" when a connected kind supplies the facet: then an empty facet means
        nothing is declared or found. Otherwise names the connections to add,
        or the connected ones whose credential does not read it.
        """

        from control_plane.observations import OBSERVATIONS
        from control_plane.connection_kinds import CONNECTION_LABELS

        from .connections import connection_rows

        kinds: list[str] = []
        providers: list[str] = []
        for kind, provider in PROVIDERS.items():
            if provider.facet == self.id and not provider.unobserved_reason:
                kinds.append(kind)
                providers.extend(provider.connection_providers)
        for kind, spec in OBSERVATIONS.items():
            if spec.facet == self.id:
                kinds.append(kind)
                providers.append(spec.provider)
        if not providers or connected_kinds() & set(kinds):
            return ""
        held = {row.provider for row in connection_rows()}
        missing = [CONNECTION_LABELS.get(p, p) for p in dict.fromkeys(providers) if p not in held]
        if missing:
            return f"Connect {' or '.join(missing)} to see it."
        unread = [CONNECTION_LABELS.get(p, p) for p in dict.fromkeys(providers)]
        return f"Not read through {' or '.join(unread)} yet."

    @property
    def declarable(self) -> tuple[tuple[str, str], ...]:
        """``(kind, label)`` for each provider that could supply this facet.

        Read from the registry rather than listed here, so the offer to add one
        appears for a provider declared long after this was written. Only kinds
        that can be seeded from a hostname.

        A certificate is offered too, only for a facet nothing supplies, so a
        name already covered is never invited to grow one of its own. Each
        offer carries the provider's label, never its identifier.
        """

        public_first = self._in_public_zone()
        return tuple(
            (kind, label)
            for _first, kind, label in sorted(
                # Only the first letter is lowered. Lowercasing the whole
                # label turned "Internal DNS record" into "internal dns
                # record" and shouted at nobody about the acronym.
                (
                    not (public_first and provider.public_effect),
                    kind,
                    lower_first(kind_label(kind)),
                )
                for kind, provider in PROVIDERS.items()
                if provider.facet == self.id
                and provider.seed is not None
                and not self._refused(provider)
            )
        )

    def _in_public_zone(self) -> bool:
        """Whether the name sits in a public zone HQ holds: one a connected
        credential edits, or one a public zone declaration manages. There a
        public provider is offered first."""

        from .naming import public_zones_declared

        name = self.context.hostname
        if not name:
            return False
        return bool(zone_holding(name, {*self.context.public_zones, *public_zones_declared()}))

    @property
    def unavailable(self) -> tuple[tuple[str, str], ...]:
        """``(label, reason)`` for providers this name rules out.

        Said rather than silently dropped. A `.home.arpa` service losing its
        Let's Encrypt option without explanation looks like a missing feature,
        and the sentence is what turns it into an answer: it names the
        alternative that does work.
        """

        return tuple(
            sorted(
                (kind_label(kind), refused)
                for kind, provider in PROVIDERS.items()
                if provider.facet == self.id
                and provider.seed is not None
                and (refused := self._refused(provider))
            )
        )

    def _refused(self, provider) -> str:
        if provider.applies is None:
            return ""
        try:
            return provider.applies(self.context)
        except (KeyError, TypeError, ValueError):
            return ""

    @property
    def routes(self) -> bool:
        """Whether providers of this facet exist to say where a name is served.

        Read from the registry: a provider that declares an ``origin`` hook is
        one whose job includes answering "and then what serves it". Tells a
        facet that is genuinely missing from one that cannot apply, because a
        name resolving straight to something outside is already routed and needs
        nothing on this network to answer for it.
        """

        return any(
            provider.origin is not None
            for provider in PROVIDERS.values()
            if provider.facet == self.id
        )

    @property
    def state(self) -> str:
        """``good``, ``attention`` or ``serious``: blank when nothing supplies it.

        Blank rather than a state, because an absence is not a health reading.
        Colouring "no certificate declared" as a failure would claim HQ had
        looked at something and found it wrong, when in fact there is nothing to
        look at, and the two call for different reactions.
        """

        if not self.claims:
            return ""
        states = {claim.health["state"] for claim in self.claims}
        if "degraded" in states:
            return "serious"
        return "attention" if states - {"healthy"} else "good"


def zone_holding(hostname: str, zones) -> str:
    """The most specific of ``zones`` holding ``hostname``, or ""."""

    return next(
        (name for name in sorted(zones, key=len, reverse=True) if in_zone(hostname, name)),
        "",
    )


def connected_kinds() -> frozenset[str]:
    """Inventory kinds a connected credential reads, once per projection."""

    return read_once(
        "services.connected_kinds",
        lambda: frozenset(
            ProviderInventory.objects.filter(connected=True).values_list("kind", flat=True)
        ),
    )


RUNTIME_FACET = "runtime"
DNS_FACET = "dns"
CERTIFICATE_FACET = "certificate"

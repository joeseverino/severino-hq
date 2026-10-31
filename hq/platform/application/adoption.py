"""Taking on what HQ found: which records it may adopt, and adopting them.

``unmanaged`` lists live records no declaration claims; ``adopt`` turns one
into a declaration, and ``adopt_service`` does so for every record a name has.
Which connections may adopt, and the ``NotManaged`` row a declaration an
operator stops managing leaves, are docs/Derived-Facts.md#adoption.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import cached_property
from typing import Annotated, Any

from django.db import transaction
lazy from django.core.exceptions import ValidationError
lazy from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, NotManaged, ProviderConnection, ProviderInventory
from hq.domains.control_plane.names import normalized_hostname
from hq.domains.control_plane.providers import PROVIDERS, registry_label, service_facets

from .command_fields import Describe, Names
from .conditions import stamped
from .inventory import record_identity, record_token, service_hostnames
from .record_errors import PolicyError, RecordNotFound
from .security import Principal
lazy from .connections import connection_rows
lazy from .infrastructure import (
    ManagedResourceCommand,
    enabled_resources,
    save_managed_resource,
    suggest_key,
)

# The capabilities that adopt, by name, for the pages that offer them.
ADOPT = "infrastructure.resource.adopt"
ADOPT_SERVICE = "infrastructure.service.adopt"

OBSERVES_ONLY = "This connection only reads. Its setup guide says how to let HQ make changes through it."


def manages_through(
    connections: Iterable[ProviderConnection] | None = None,
) -> Callable[[str, str], bool]:
    """A test of whether a record of ``kind`` read through ``ref`` may be adopted.

    A record naming its connection needs that connection to manage. A record
    naming none needs every connection of the kind's providers to manage, and
    at least one to exist: which of them read it is not known.
    """

    return _Management(connections)


class _Management:
    """Whether each connection manages, by provider, read on first use so a
    caller with nothing to ask costs no query."""

    def __init__(self, connections: Iterable[ProviderConnection] | None) -> None:
        self._connections = connections

    @cached_property
    def _by_provider(self) -> dict[str, list[tuple[str, bool]]]:
        rows = connection_rows() if self._connections is None else self._connections
        found: dict[str, list[tuple[str, bool]]] = {}
        for row in rows:
            found.setdefault(row.provider, []).append((row.connection_ref, row.manages))
        return found

    def __call__(self, kind: str, connection_ref: str = "") -> bool:
        return self._named_manages(connection_ref) if connection_ref else self._every_of_kind_manages(kind)

    def _named_manages(self, connection_ref: str) -> bool:
        return any(ref == connection_ref and flag for rows in self._by_provider.values() for ref, flag in rows)

    def _every_of_kind_manages(self, kind: str) -> bool:
        provider = PROVIDERS.get(kind)
        if provider is None:
            return False
        found = [flag for name in provider.connection_providers for _, flag in self._by_provider.get(name, ())]
        return bool(found) and all(found)


def observes_only(kind: str, spec, manages=None) -> bool:
    """Whether a declaration of ``kind`` acts through a connection that only observes.

    ``manages`` is a ``manages_through()`` test, shared by a caller asking about
    many declarations; left as None it is built here. A kind that acts through
    no connection, or never acts (declaration-only), never observes only.
    """

    provider = PROVIDERS.get(kind)
    if provider is None or not provider.connection_providers or provider.declaration_only:
        return False
    test = manages or manages_through()
    return not test(kind, str((spec or {}).get("connection_ref", "")))


def kept_out() -> frozenset[tuple[str, str]]:
    """``(kind, token)`` for every record an operator said HQ does not manage."""

    return frozenset(NotManaged.objects.values_list("kind", "token"))


def keep_out(kind: str, token: str, label: str, *, principal) -> None:
    """Record that HQ does not manage this record. Idempotent."""

    NotManaged.objects.get_or_create(
        kind=kind,
        token=token,
        defaults={
            "label": label[:300],
            "actor": principal.actor[:160],
        },
    )


def keep_out_declaration(kind: str, spec: dict, *, principal) -> None:
    """Keep out the record a forgotten declaration described.

    Only kinds a sweep can adopt; anything else has nothing to re-adopt.
    """

    provider = PROVIDERS.get(kind)
    if provider is None or provider.from_record is None:
        return
    identity = record_identity(kind, spec)
    if not identity:
        return
    keep_out(
        kind,
        record_token(kind, identity),
        " ".join(str(part) for part in identity),
        principal=principal,
    )


def let_in(kind: str, token: str) -> None:
    """Clear an operator's earlier choice, so the record is managed again."""

    for row in NotManaged.objects.filter(kind=kind, token=token):
        # Deleted per row so the audit signal records each.
        row.delete()


@dataclass(frozen=True, slots=True)
class Unmanaged:
    """One record a provider holds that no HQ declaration accounts for.

    ``identity`` is what makes it that record; ``hostnames`` is what it serves.
    They are the same for a rewrite or a proxy host and deliberately different
    for a DNS record, which may be one of nine on a single name and may serve
    nothing at all.
    """

    kind: str
    identity: tuple[str, ...]
    hostnames: tuple[str, ...]
    spec: dict[str, Any]
    observed_at: Any
    # False when the provider will not take this record on unasked: it stays
    # here, and a finding says so, until a person adopts or removes it.
    adoptable: bool = True
    # The connection that read it, when the record names one.
    connection_ref: str = ""
    # True unless it was read through a connection that manages and its kind
    # takes found records on. An observed record is shown and never adopted.
    # See ``application.adoption``.
    observed_only: bool = True

    @property
    def label(self) -> str:
        return registry_label(self.kind)

    @property
    def hostname(self) -> str:
        return self.hostnames[0] if self.hostnames else ""

    @property
    def token(self) -> str:
        """A short, stable handle for this exact record, safe to put in a URL.

        Derived rather than stored because nothing persists an unmanaged record
        it exists only in the last sweep. Hashed rather than joined because
        an identity contains a DNS value, and a TXT record's value is neither
        short nor URL-safe.
        """

        return record_token(self.kind, self.identity)

    @property
    def readout(self) -> tuple[tuple[str, str], ...]:
        """What this record does, described by its own provider.

        Nothing outside a provider reads its spec fields: record kinds differ in
        shape, so each provider says how to describe itself.
        """

        provider = PROVIDERS[self.kind]
        if provider.readout is None:
            return ()
        try:
            rows = provider.readout(self.spec, {})
        except KeyError, TypeError, ValueError:
            return ()
        return tuple((label, str(desired)) for label, desired, _ in rows if desired)


def unmanaged() -> tuple[Unmanaged, ...]:
    """Records a provider holds that no enabled declaration accounts for.

    Matched on hostname rather than on any provider id, because that is how the
    reconcilers find their own records. A declaration and a live record with the
    same hostnames are the same thing by the only definition that governs what
    actually happens.
    """

    declared: dict[str, set[tuple[str, ...]]] = {}
    for resource in enabled_resources():
        if resource.kind not in PROVIDERS:
            continue
        declared.setdefault(resource.kind, set()).add(record_identity(resource.kind, resource.spec))

    manages = manages_through()
    found: list[Unmanaged] = []
    for snapshot in ProviderInventory.objects.all():
        provider = PROVIDERS.get(snapshot.kind)
        if provider is None or provider.from_record is None:
            continue
        known = declared.get(snapshot.kind, set())
        for record in snapshot.records:
            try:
                spec = provider.from_record(record)
            except KeyError, TypeError, ValueError:
                continue
            identity = record_identity(snapshot.kind, spec)
            if not identity or identity in known:
                continue
            connection_ref = str(record.get("connection_ref", "") or "")
            found.append(
                Unmanaged(
                    kind=snapshot.kind,
                    identity=identity,
                    hostnames=service_hostnames(snapshot.kind, spec),
                    spec=spec,
                    observed_at=snapshot.observed_at,
                    adoptable=not provider.adoption_gap and (provider.adopts is None or provider.adopts(record)),
                    connection_ref=connection_ref,
                    observed_only=bool(provider.adoption_gap) or not manages(snapshot.kind, connection_ref),
                )
            )
    return tuple(sorted(found, key=lambda item: (item.identity, item.kind)))


@dataclass(frozen=True, slots=True)
class UnmanagedService:
    """Every unmanaged record sharing one hostname, seen as one thing.

    Grouped because a hostname is the unit an operator thinks in, and because
    the managed table beside this one is already per-hostname: one service is
    one row and one adoption.
    """

    hostname: str
    items: tuple[Unmanaged, ...]

    @property
    def observed_only(self) -> bool:
        """Whether every record behind the name is read only to observe."""

        return all(item.observed_only for item in self.items)

    @property
    def observed_at(self):
        return max(item.observed_at for item in self.items)

    @property
    def facets(self) -> tuple[tuple[str, str, str], ...]:
        """``(id, label, value)`` per facet, lining up with the managed table.

        The value only. Each readout row carries its own label: "Answers with",
        "Forwards to", which is right on a detail card that has no column
        headings, and pure noise in a table whose column already says DNS. The
        secondary rows go the same way: what a list is for is scanning where a
        name points, and the rest is one click away.
        """

        by_facet = {
            PROVIDERS[item.kind].facet: item.readout
            for item in self.items
            if PROVIDERS[item.kind].facet and item.readout
        }
        return tuple(
            (
                facet_id,
                label,
                by_facet.get(facet_id, (("", ""),))[0][1],
            )
            for facet_id, label in service_facets()
        )


def unmanaged_services() -> tuple[UnmanagedService, ...]:
    """Unmanaged records grouped by the service they serve.

    Records that serve no hostname are deliberately absent. A DMARC policy and a
    CAA record are real, unmanaged and worth adopting, but they are not services
    and grouping them here would file every one of them under a service whose
    name is the empty string.
    """

    grouped: dict[str, list[Unmanaged]] = {}
    for item in unmanaged():
        if not item.hostname:
            continue
        grouped.setdefault(item.hostname, []).append(item)
    return tuple(UnmanagedService(hostname=hostname, items=tuple(items)) for hostname, items in sorted(grouped.items()))


def find_unmanaged(kind: str, hostname: str = "", *, token: str = "") -> Unmanaged | None:
    """One unmanaged record, found by exact identity or by the name it serves.

    Both, because both questions are asked. "Adopt this service" means every
    record behind a hostname; "adopt this record" means one row of a zone, which
    may share its hostname with eight others and may serve nothing at all.
    """

    candidates = [item for item in unmanaged() if item.kind == kind]
    if token:
        return next((item for item in candidates if item.token == token), None)
    wanted = normalized_hostname(hostname)
    return next((item for item in candidates if wanted in item.hostnames), None)


@dataclass(frozen=True, slots=True)
class AdoptServiceCommand:
    hostname: Annotated[str, Names("services"), Describe("The hostname whose live records to manage in HQ.")]


@transaction.atomic
def adopt_service(
    command: AdoptServiceCommand,
    *,
    principal: Principal,
) -> dict[str, Any]:
    """Adopt every unmanaged record behind one hostname, or none of them.

    A hostname is the unit an operator is thinking about: its DNS record and
    the proxy host in front of it are one decision, not two. Atomic because a
    half-adopted service is worse than an unadopted one: HQ would manage the
    name's ingress while its DNS answer stayed outside, and the service page
    would show a gap that is not really there.
    """

    found = next(
        (service for service in unmanaged_services() if service.hostname == normalized_hostname(command.hostname)),
        None,
    )
    if found is None:
        raise RecordNotFound("HQ found nothing new to manage for that name. It may be managed already, or gone.")

    writable = [item for item in found.items if not item.observed_only]
    if not writable:
        raise PolicyError("HQ reads that name only through connections that only read.")
    adopted = [
        adopt(
            # By token, not by hostname: a service may be served by several
            # records of one kind, and adopting by name would adopt the first
            # one repeatedly and silently skip the rest.
            AdoptCommand(kind=item.kind, token=item.token),
            principal=principal,
        )["resource"]["key"]
        for item in writable
    ]
    return {"ok": True, "hostname": found.hostname, "adopted": adopted}


@dataclass(frozen=True, slots=True)
class AdoptCommand:
    kind: Annotated[str, Describe("The type of record to bring under HQ's management.")]
    hostname: Annotated[str, Describe("The hostname whose record to adopt.")] = ""
    key: Annotated[str, Describe("The key the managed record is stored under; blank derives one.")] = ""
    # Set when adopting one specific record rather than everything a hostname
    # answers with. Takes precedence: it identifies exactly one row, where a
    # hostname may match several.
    token: Annotated[str, Describe("The token HQ gave the one live record when it read it.")] = ""


def adopt(
    command: AdoptCommand,
    *,
    principal: Principal,
) -> dict[str, Any]:
    """Bring a record the provider already holds under HQ's management.

    The spec is the live record, read again here rather than posted by a form
    that could carry a stale or edited copy, so adopting asserts nothing new:
    the resource is born in sync and its first reconciliation is a no-op.
    """

    found = find_unmanaged(command.kind, command.hostname, token=command.token)
    if found is None:
        raise RecordNotFound("HQ found no new record of that type to manage. It may be managed already, or gone.")
    provider = PROVIDERS[found.kind]
    if provider.adoption_gap:
        raise PolicyError(provider.why_not_adoptable())
    if found.observed_only:
        raise PolicyError(
            "This record is read through a connection that only reads. "
            "Its setup guide says how to let HQ make changes through it, which adopting needs."
        )
    # Every other write's capability check, spec validation, fingerprint and
    # audit record, rather than a row created here.
    result = save_managed_resource(
        ManagedResourceCommand(
            key=command.key or suggested_key(found),
            kind=found.kind,
            spec=found.spec,
            enabled=True,
        ),
        principal=principal,
        copied_from_live=True,
    )
    _record_as_observed(result.get("resource", {}).get("key", ""), found)
    # Adopting is the operator managing it again.
    let_in(found.kind, found.token)
    return result


def _record_as_observed(key: str, found: Unmanaged) -> None:
    """Mark an adopted resource as seen, because it just was.

    A typed declaration is born unobserved; an adopted one was read from the
    live record moments ago. Left unmarked it would say "never reported"
    forever, since nothing queues a reconcile for a resource that has not
    drifted, and a service assembled from it would read as incomplete.
    """

    resource = ManagedResource.objects.filter(key=key).first()
    if resource is None:
        return
    resource.observed_generation = resource.generation
    resource.last_observed_at = timezone.now()
    # What was found, which for an adopted resource is what was declared.
    resource.status = dict(found.spec)
    resource.conditions = stamped(
        resource.conditions,
        [
            {
                "type": "Ready",
                "status": True,
                "reason": "Adopted",
                "message": "Added to HQ from what was live.",
            }
        ],
    )
    resource.save(
        update_fields=[
            "observed_generation",
            "last_observed_at",
            "status",
            "conditions",
        ]
    )


def suggested_key(item: Unmanaged) -> str:
    """A free key an operator would recognize on a list of declarations."""

    return suggest_key(item.kind, item.spec)


def adopt_discovered(kind: str, *, principal) -> dict[str, Any]:
    """Take on every record of one kind that no declaration accounts for.

    Only records read through a connection that manages, and none an operator
    said HQ does not manage. The connection's ``manages`` is the decision; a
    connection that only observes adopts nothing.
    """

    excluded = kept_out()
    adopted: list[str] = []
    for item in unmanaged():
        if item.kind != kind or not item.adoptable or item.observed_only:
            continue
        if (item.kind, item.token) in excluded:
            continue
        try:
            result = adopt(AdoptCommand(kind=item.kind, token=item.token), principal=principal)
        except RecordNotFound, PolicyError, ValidationError, ValueError:
            # One record that cannot be adopted must not stop the rest. The
            # next sweep tries again, so this closes itself rather than needing
            # anybody to notice.
            continue
        adopted.append(result.get("resource", {}).get("key", ""))
    return {"adopted": [key for key in adopted if key]}

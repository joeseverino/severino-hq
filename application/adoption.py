"""Which records HQ may take on, and which an operator has kept out.

A record is adopted only through a connection that manages: one whose item
declares ``manages`` (rendered as ``<PREFIX>_MANAGES=1``). Every other
connection observes. What it reads is shown as observed and never declared.

A declaration an operator stops managing leaves a ``NotManaged`` row. Adoption
skips that record until an operator manages it again, which clears the row.
Both are audited through the model's registration.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from control_plane.models import NotManaged, ProviderConnection
from control_plane.providers import PROVIDERS


OBSERVES_ONLY = "Its connection only observes. Set manages on the connection to act."


def manages_through(
    connections: Iterable[ProviderConnection] | None = None,
) -> Callable[[str, str], bool]:
    """A test of whether a record of ``kind`` read through ``ref`` may be adopted.

    A record naming its connection needs that connection to manage. A record
    naming none needs every connection of the kind's providers to manage, and
    at least one to exist: which of them read it is not known.
    """

    # Read on first use, so a caller with nothing to ask costs no query.
    loaded: list[dict[str, list[tuple[str, bool]]]] = []

    def by_provider() -> dict[str, list[tuple[str, bool]]]:
        if not loaded:
            from .connections import connection_rows

            rows = connection_rows() if connections is None else connections
            found: dict[str, list[tuple[str, bool]]] = {}
            for row in rows:
                found.setdefault(row.provider, []).append(
                    (row.connection_ref, row.manages)
                )
            loaded.append(found)
        return loaded[0]

    def manages(kind: str, connection_ref: str = "") -> bool:
        known = by_provider()
        if connection_ref:
            return any(
                ref == connection_ref and flag
                for rows in known.values()
                for ref, flag in rows
            )
        provider = PROVIDERS.get(kind)
        if provider is None:
            return False
        found = [
            flag
            for name in provider.connection_providers
            for _, flag in known.get(name, ())
        ]
        return bool(found) and all(found)

    return manages


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
            "actor": str(getattr(principal, "actor", "") or "")[:160],
        },
    )


def keep_out_declaration(kind: str, spec: dict, *, principal) -> None:
    """Keep out the record a forgotten declaration described.

    Only kinds a sweep can adopt; anything else has nothing to re-adopt.
    """

    from .inventory import _identity, record_token

    provider = PROVIDERS.get(kind)
    if provider is None or provider.from_record is None:
        return
    identity = _identity(kind, spec)
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

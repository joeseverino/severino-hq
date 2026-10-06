"""What a connection's credential lacks, and the one command that replaces it.

HQ holds no credential that can create tokens, so it never mints. It derives
what the credential needs (the declared ``requires`` of every reading it feeds,
``control_plane.credential_reads``), what it lacks (the readings the provider
refused while the credential itself is valid), when it expires (the probe's
reading), and the command an operator runs on their own machine to mint a
replacement and store it in the connection's 1Password item.

The command is built from references only: the script, the account id the
readings name, and ``op://`` references to the connection item and its
bootstrap item. No value HQ holds is a secret, so no secret can reach it.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from functools import cache
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from hq.domains.control_plane.credential_reads import MINTERS, Minter, observer_permissions
from hq.domains.control_plane.observations import OBSERVATIONS

from .projection import read_once
from .timestamps import moment

# How far ahead of expiry a credential is replaced.
RENEWAL_WINDOW = timedelta(days=30)

_ITEM_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_NAME = re.compile(r"^[^\x00-\x1f\x7f/]{1,200}$")
_BOOTSTRAP = re.compile(r"^op://([^\x00-\x1f\x7f/]{1,200})/([^\x00-\x1f\x7f/]{1,200})$")
_ACCOUNT = re.compile(r"^[A-Za-z0-9]{1,64}$")


def store_references(raw: Any) -> dict[str, str]:
    """The ``vault``, ``item`` and ``bootstrap`` references a controller reported.

    Anything else is dropped, and so is a value that is not a reference: this
    field carries names, never values.
    """

    if not isinstance(raw, Mapping):
        return {}
    vault = str(raw.get("vault") or "").strip()
    item = str(raw.get("item") or "").strip()
    bootstrap = str(raw.get("bootstrap") or "").strip()
    found: dict[str, str] = {}
    if _NAME.match(vault) and _ITEM_ID.match(item):
        found.update(vault=vault, item=item)
    match = _BOOTSTRAP.match(bootstrap)
    # A bootstrap the controller's own vault holds is one the controller reads.
    # 1Password resolves a vault name without regard to case.
    if match and match.group(1).strip().casefold() != vault.casefold():
        found["bootstrap"] = bootstrap
    return found


def parse_expiry(raw: Any) -> datetime | None:
    """A reported expiry as an aware datetime, or None."""

    return moment(raw, naive="refuse")


@cache
def _projections() -> dict[str, dict[str, Any]]:
    from hq.domains.control_plane.connection_shapes import projections

    return projections()


@cache
def projection_field(variable: str) -> str:
    """The item field a projection variable is rendered from, the same in every
    projection that has it."""

    fields = {
        entry.get("id") or entry.get("label")
        for projection in _projections().values()
        for name, entry in projection.items()
        if name == variable and entry.get("source") == "field"
    }
    if len(fields) != 1:
        raise ImproperlyConfigured(
            f"{variable} is rendered from {len(fields)} different fields; a stored "
            "credential needs exactly one."
        )
    return fields.pop()


# The variable a connection's address is rendered into.
ADDRESS_VARIABLE = "URL"


@cache
def address_fields() -> str:
    """Where a connection item holds its address, in words, from the projections:
    the field it is rendered from, or the item's own URL, naming the projections
    when they differ."""

    sources: dict[str, list[str]] = {}
    for name, projection in sorted(_projections().items()):
        entry = projection.get(ADDRESS_VARIABLE)
        if not entry:
            continue
        if entry.get("source") == "field":
            where = f"its {projection_field(ADDRESS_VARIABLE)} field"
        else:
            where = "its first URL" if not entry.get("index") else "one of its URLs"
        sources.setdefault(where, []).append(name)
    if len(sources) == 1:
        return next(iter(sources))
    return " or ".join(
        f"{where} ({', '.join(names)} items)" for where, names in sources.items()
    )


@dataclass(frozen=True)
class CredentialFix:
    """One connection's credential: what it lacks, and how it is replaced."""

    connection_ref: str
    provider: str
    # Permissions the provider refused while the credential itself is valid.
    missing: tuple[str, ...] = ()
    # What those permissions would let HQ see.
    unseen: tuple[str, ...] = ()
    # The provider's words when it refused the credential outright.
    refused: str = ""
    expires_at: datetime | None = None
    # Every permission the replacement is minted with.
    permissions: tuple[str, ...] = ()
    # The operator command, or "" when a part of it is not known yet.
    command: str = ""
    # What HQ could not derive, each as a sentence.
    gaps: tuple[str, ...] = ()
    by_hand: str = ""

    @property
    def expiring(self) -> bool:
        return self.expires_at is not None and self.expires_at - timezone.now() <= RENEWAL_WINDOW

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= timezone.now()

    @property
    def needed(self) -> bool:
        return bool(self.missing or self.refused or self.expiring)

    @property
    def expiry(self) -> str:
        from hq.domains.control_plane.provider_spec import expiry_phrase

        return expiry_phrase(self.expires_at.isoformat()) if self.expires_at else ""

    @property
    def steps(self) -> tuple[Any, ...]:
        from .credential_findings import fix_steps

        return fix_steps(self)


def _accounts(provider: str) -> tuple[str, ...]:
    """Every account id the provider's readings name."""

    from .facts import snapshots_of

    return tuple(
        sorted(
            {
                str(record.get("account_id"))
                for kind, spec in OBSERVATIONS.items()
                if spec.provider == provider
                for snapshot in snapshots_of(kind)
                for record in snapshot.records or ()
                if isinstance(record, dict) and _ACCOUNT.match(str(record.get("account_id") or ""))
            }
        )
    )


def _reference(vault: str, item: str, variable: str) -> str:
    return f"op://{vault}/{item}/{projection_field(variable)}"


def mint_command(
    minter: Minter, provider: str, store: Mapping[str, str]
) -> tuple[str, tuple[str, ...]]:
    """The operator command, or "", and what HQ could not derive for it.

    A missing account or item leaves no command: guessing either would mint a
    credential for the wrong place. A missing bootstrap item leaves the command
    reading the bootstrap from the environment, and says so.
    """

    blocking: list[str] = []
    notes: list[str] = []
    arguments = [f"./{minter.script}"]
    if minter.account_flag:
        accounts = _accounts(provider)
        if len(accounts) == 1:
            arguments += [minter.account_flag, accounts[0]]
        else:
            blocking.append(
                "HQ has not read the account id yet."
                if not accounts
                else f"HQ has read {len(accounts)} accounts and cannot tell which one this is."
            )
    vault, item = store.get("vault", ""), store.get("item", "")
    if vault and item:
        for flag, variable in minter.stores:
            arguments += [flag, _reference(vault, item, variable)]
    else:
        blocking.append(
            "The controller has not said which 1Password item holds this "
            "token. It will once it is up to date."
        )
    prefix: list[str] = []
    bootstrap = _BOOTSTRAP.match(store.get("bootstrap", ""))
    if bootstrap:
        prefix = [
            f"{name}={shlex.quote(_reference(bootstrap[1], bootstrap[2], variable))}"
            for name, variable in minter.bootstrap
        ] + ["op", "run", "--"]
    else:
        names = " and ".join(name for name, _ in minter.bootstrap)
        notes.append(
            f"The connection's 1Password item names no bootstrap item. Set {names} "
            "first, or add a bootstrap field (op://<vault>/<item>) to the item."
        )
    if blocking:
        return "", tuple(blocking + notes)
    return " ".join([*prefix, *(shlex.quote(part) for part in arguments)]), tuple(notes)


def _fix(row: Any, sight: Any) -> CredentialFix:
    minter = MINTERS.get(row.provider)
    fix = CredentialFix(
        connection_ref=row.connection_ref,
        provider=row.provider,
        missing=sight.missing if sight else (),
        unseen=sight.unseen if sight else (),
        refused=sight.credential_refusal if sight else "",
        expires_at=row.expires_at,
        permissions=observer_permissions(row.provider),
    )
    if minter is None or not fix.needed:
        return fix
    command, gaps = mint_command(minter, row.provider, store_references(row.store))
    return replace(fix, command=command, gaps=gaps, by_hand=minter.by_hand)


def credential_fixes() -> dict[str, CredentialFix]:
    """Each reported connection's credential fix, by connection ref."""

    def load() -> dict[str, CredentialFix]:
        from .connections import connection_rows
        from .credential_sight import credential_sight

        sights = {found.provider: found for found in credential_sight()}
        return {
            row.connection_ref: _fix(row, sights.get(row.provider))
            for row in connection_rows()
        }

    return read_once("credential_fixes", load)

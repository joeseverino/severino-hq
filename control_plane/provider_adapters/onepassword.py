"""Records what HQ observes about a certificate where credentials are kept.

A password manager is the first place an operator looks to ask "what is this,
and when does it run out". For every other credential the answer is already
there; for a certificate it was only ever in HQ, so the two had to be read side
by side. This writes HQ's answer onto the item, so one look answers it.

It delivers no certificate. The material reaches the machines that serve it over
the paths that already carry it, and nothing here reads or writes a file, an
attachment, or a private key. What travels through here is five short strings
about a certificate, all of which are public the moment it is served.

That is what makes the write-capable credential defensible, and the shape of
this module is the argument: the declaration says *where*, and the code says
*what*. See ``publish``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from .contracts import ProviderError, ProviderRuntime


# The complete set of fields this adapter will ever write, in the order an
# operator reads them, each with the type `op` should store it as. A label is a
# question about the certificate; the value is HQ's own answer, taken from what
# it observed. Nothing outside this module contributes to either.
#
# Human words rather than field names, because the audience is a person reading a
# 1Password item and not a program parsing one.
#
# The expiry is a real date rather than a string that looks like one, and that is
# the point of it. Typed, the credential store itself knows when the certificate
# goes stale -- it renders and sorts it as a date, and can be asked -- so the
# warning survives HQ being down, misconfigured, or quietly not sweeping. That is
# not hypothetical: a copy of a certificate here sat two months expired because
# the only thing that knew the date was the renewal path, and nobody was reading
# it.
PUBLISHED_FIELDS: Mapping[str, str] = MappingProxyType(
    {
        "Covers": "text",
        "Issued by": "text",
        "Expires": "date",
        "Fingerprint (SHA-256)": "text",
        "Installed on": "text",
    }
)
PUBLISHED_LABELS = tuple(PUBLISHED_FIELDS)

# What `op` calls each of those when it reads the item back. The assignment
# keyword and the stored type are not the same word -- `[text]` is stored as
# `STRING` -- so a comparison that assumed they were would find every text field
# different on every pass and rewrite all of them forever.
_STORED_AS: Mapping[str, str] = MappingProxyType({"text": "STRING", "date": "DATE"})

# Stamped on every item this adapter writes, so the item says who maintains it.
# The query worth having is the inverse: an item in these vaults *without* this
# tag is one a person put there and nothing keeps up to date, which is how the
# next silently expired copy gets found before it matters.
#
# A flat adjective rather than the `severino-hq.role=controller` namespace the
# containers use. That convention is a key and a value on a system that has
# them; a tag is one word, and the useful reading here is a plain claim of
# ownership rather than a role within it.
MANAGED_TAG = "hq-managed"


def _fingerprint(status: dict[str, Any]) -> str:
    """The one fingerprint HQ can say this certificate is.

    After a deployment the expected fingerprint is stated outright. After a
    plain observation there is only what each consumer served, so it is the
    answer when they agree and no answer at all when they do not -- a
    disagreement is what a drift condition is for, and averaging it into one
    published fact would record a certainty HQ does not have.
    """

    expected = str(status.get("expected_fingerprint_sha256", "") or "")
    if expected:
        return expected
    observed = {
        str(item.get("fingerprint_sha256", "") or "")
        for item in status.get("consumers", ())
    }
    return observed.pop() if len(observed) == 1 else ""


def facts(spec: dict[str, Any], status: dict[str, Any]) -> dict[str, str]:
    """The fields to write, decided here and derived from HQ's own observation.

    Every value comes from the certificate HQ resolved and the reading it just
    took. None of them comes from the delivery target, so a declaration cannot
    reach into this: it has no field that could carry a value, a label, or a
    sixth fact, and this function never looks at one.

    Returns nothing at all when the fingerprint is unknown, rather than a
    partial item. The five are one statement about one certificate, and four of
    them beside a stale fifth is the kind of record that gets trusted wrongly.
    """

    fingerprint = _fingerprint(status)
    if not fingerprint:
        return {}
    expires = str(status.get("not_after", "") or "")
    return {
        "Covers": ", ".join(spec.get("domains", ())),
        "Issued by": str(status.get("issuer", "") or ""),
        # The date alone. The timestamp carries a timezone and a microsecond,
        # and a person reading an item wants the day.
        "Expires": expires[:10],
        "Fingerprint (SHA-256)": fingerprint,
        "Installed on": ", ".join(
            sorted(
                str(consumer.get("name", ""))
                for consumer in spec.get("consumers", ())
                if consumer.get("name")
            )
        ),
    }


def _token(runtime: ProviderRuntime, connection_ref: str) -> str:
    prefix = runtime.connection_prefix("onepassword", connection_ref)
    return runtime.required(prefix, "API_TOKEN")


def _current(
    runtime: ProviderRuntime, publication: dict[str, Any], token: str
) -> tuple[dict[str, tuple[str, str]], tuple[str, ...]]:
    """What the item already says, and every tag it already carries.

    Read before writing so an unchanged certificate costs no write at all. Every
    other field on the item is read past and left alone -- the item exists for
    the operator's own reasons and this adapter is a guest on it.

    Each owned field comes back as its stored *type* beside its value, because a
    field whose value is right and whose type is wrong is still wrong: the expiry
    written as text before this adapter typed it reads identically and is not a
    date to anything that asks. Compared on the value alone it would never be
    corrected.

    The tags come back whole rather than filtered, and that is load-bearing.
    ``op item edit --tags`` replaces the list instead of adding to it, so writing
    only this adapter's tag would silently drop every tag a person had put on the
    item.
    """

    raw = runtime.run(
        [
            "op",
            "item",
            "get",
            publication["item"],
            "--vault",
            publication["vault"],
            "--format",
            "json",
        ],
        env={"OP_SERVICE_ACCOUNT_TOKEN": token},
        step=f"1Password read for {publication['name']}",
    )
    try:
        document = json.loads(raw or b"{}")
    except ValueError as exc:
        raise ProviderError("1Password returned an item HQ could not read.") from exc
    if not isinstance(document, dict):
        raise ProviderError("1Password returned an item HQ could not read.")
    fields = {
        str(field.get("label", "")): (
            str(field.get("type", "") or ""),
            str(field.get("value", "") or ""),
        )
        for field in document.get("fields") or ()
        if str(field.get("label", "")) in PUBLISHED_FIELDS
    }
    tags = tuple(
        str(tag) for tag in document.get("tags") or () if str(tag).strip()
    )
    return fields, tags


def publish(
    runtime: ProviderRuntime, publication: dict[str, Any], desired: dict[str, str]
) -> dict[str, Any]:
    """Write HQ's facts onto one item, and only if they have changed.

    THE DECLARATION SAYS WHERE. THIS CODE SAYS WHAT.

    ``publication`` supplies the connection, the vault and the item: three
    pieces of addressing and nothing else. ``desired`` is built by ``facts``
    from the certificate HQ resolved and the certificate HQ just observed. So
    the set of fields, their labels and their values are all fixed in this
    module, and a declaration -- which is operator input, held in HQ's database,
    editable through a form -- cannot name an extra field, supply a value, or
    point this at different content. That is the reason a token that can write
    is safe to hand to this path: the worst a wrong declaration can do is write
    five true facts onto the wrong item.

    Three things it does not do, each deliberate:

    * No attachments, read or written. The certificate and its key travel to the
      machines that serve them over the paths that already carry them; this is
      metadata, and a file here would make it a second copy of a secret.
    * No deletes. Not a field, not the item. It writes the labels above and
      leaves every other field, and the item itself, exactly as it found them.
    * No note. The note is where a person writes things, and an automated writer
      that owns it will eventually overwrite something that was not its to
      touch.

    The item is tagged as HQ's, and the tag list is written as the union of what
    was already there with this adapter's own. ``--tags`` replaces rather than
    appends, so anything less than the union would quietly drop a tag the owner
    had added by hand.

    Idempotent by comparison rather than by hope: unchanged facts and a tag
    already present produce no write at all, so the ordinary case -- a
    certificate observed every pass and renewed every couple of months --
    touches 1Password twice a year.
    """

    token = _token(runtime, publication["connection_ref"])
    current, tags = _current(runtime, publication, token)
    changed = sorted(
        label
        for label, kind in PUBLISHED_FIELDS.items()
        if current.get(label) != (_STORED_AS[kind], desired[label])
    )
    tagged = MANAGED_TAG in tags
    if not changed and tagged:
        return {
            "target": publication["name"],
            "written": False,
            "fields": [],
            "tagged": True,
        }
    runtime.run(
        [
            "op",
            "item",
            "edit",
            publication["item"],
            "--vault",
            publication["vault"],
            # The union, never just this adapter's tag: `--tags` sets the whole
            # list, so writing less than this drops whatever the owner added.
            "--tags",
            ",".join(sorted({*tags, MANAGED_TAG})),
            # Assignments only, one per label this adapter owns, each carrying
            # the type it is stored as. `op` treats an assignment as an upsert of
            # that one field, so nothing else on the item is named and nothing
            # can be removed by what is left out.
            *(
                f"{label}[{PUBLISHED_FIELDS[label]}]={desired[label]}"
                for label in changed
            ),
        ],
        env={"OP_SERVICE_ACCOUNT_TOKEN": token},
        step=f"1Password write for {publication['name']}",
    )
    return {
        "target": publication["name"],
        "written": True,
        "fields": changed,
        "tagged": True,
    }


def probe(runtime: ProviderRuntime, connection_ref: str) -> dict[str, Any]:
    """Prove the service account token is still accepted, and retain nothing.

    Counted rather than named. A service account sees only the vaults it was
    granted, so the count is the credential's own statement that it has some
    reach; the names would be reported as things this connection *reaches*,
    which everywhere else in HQ means a machine. A vault is not a machine, and
    filing one as one is how a page starts describing an estate that does not
    exist.
    """

    raw = runtime.run(
        ["op", "vault", "list", "--format", "json"],
        env={"OP_SERVICE_ACCOUNT_TOKEN": _token(runtime, connection_ref)},
        step=f"1Password preflight for {connection_ref}",
    )
    try:
        vaults = json.loads(raw or b"[]")
    except ValueError as exc:
        raise ProviderError(
            "1Password returned a vault list HQ could not read."
        ) from exc
    if not isinstance(vaults, list):
        raise ProviderError("1Password returned a vault list HQ could not read.")
    return {
        "detail": f"Service account accepted; it holds {len(vaults)} vaults.",
        "reaches": [],
    }

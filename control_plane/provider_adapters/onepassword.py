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
from typing import Any

from .contracts import ProviderError, ProviderRuntime


# The complete set of labels this adapter will ever write, in the order an
# operator reads them. A label is a question about the certificate; the value is
# HQ's own answer, taken from what it observed. Anything not on this tuple is not
# written, and nothing outside this module contributes to it.
#
# Human words rather than field names, because the audience is a person reading a
# 1Password item and not a program parsing one.
PUBLISHED_LABELS = (
    "Covers",
    "Issued by",
    "Expires",
    "Fingerprint (SHA-256)",
    "Installed on",
)


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
) -> dict[str, str]:
    """What the item already says, restricted to the labels this adapter owns.

    Read before writing so an unchanged certificate costs no write at all. Every
    other field on the item is read past and left alone -- the item exists for
    the operator's own reasons and this adapter is a guest on it.
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
    return {
        str(field.get("label", "")): str(field.get("value", "") or "")
        for field in document.get("fields") or ()
        if str(field.get("label", "")) in PUBLISHED_LABELS
    }


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

    Idempotent by comparison rather than by hope: unchanged facts produce no
    subprocess and no write, so the ordinary case -- a certificate observed
    every pass and renewed every couple of months -- touches 1Password twice a
    year.
    """

    token = _token(runtime, publication["connection_ref"])
    current = _current(runtime, publication, token)
    changed = sorted(
        label for label in PUBLISHED_LABELS if current.get(label, "") != desired[label]
    )
    if not changed:
        return {"target": publication["name"], "written": False, "fields": []}
    runtime.run(
        [
            "op",
            "item",
            "edit",
            publication["item"],
            "--vault",
            publication["vault"],
            # Assignments only, one per label this adapter owns. `op` treats an
            # assignment as an upsert of that one field, so nothing else on the
            # item is named and nothing can be removed by what is left out.
            *(f"{label}[text]={desired[label]}" for label in changed),
        ],
        env={"OP_SERVICE_ACCOUNT_TOKEN": token},
        step=f"1Password write for {publication['name']}",
    )
    return {"target": publication["name"], "written": True, "fields": changed}


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

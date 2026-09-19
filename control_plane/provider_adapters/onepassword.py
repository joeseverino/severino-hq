"""Records what HQ observes about a certificate where credentials are kept.

A password manager is the first place an operator looks to ask "what is this,
and when does it run out". For every other credential the answer is already
there; for a certificate it was only ever in HQ, so the two had to be read side
by side. This writes HQ's answer onto the item, so one look answers it.

It delivers the certificate too. An item carrying the facts beside material a
person had placed by hand would be the worst of both: the facts refreshed on
renewal, the files frozen at whatever was last uploaded, and the whole thing
confident and wrong. So the same reconcile writes both, or the item is not
maintained at all.

That the controller handles the key here is not a widening. Installing this
certificate on the machines that serve it is already its job, so it holds the
key on every pass by definition. What this removes is the hand step, and the
hand step is the part that rots.

That is what makes the write-capable credential defensible, and the shape of
this module is the argument: the declaration says *where*, and the code says
*what*. See ``publish``.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping
from datetime import date
from pathlib import Path
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
# warning does not depend on HQ being up, configured, or sweeping.
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

# The one published field that is also an identity rather than a description: it
# names the certificate's content, so it decides whether what is attached is this
# certificate or an older one.
FINGERPRINT_LABEL = "Fingerprint (SHA-256)"

# What `op` calls each of those when it reads the item back. The assignment
# keyword and the stored type are not the same word -- `[text]` is stored as
# `STRING` -- so a comparison that assumed they were would find every text field
# different on every pass and rewrite all of them forever.
_STORED_AS: Mapping[str, str] = MappingProxyType({"text": "STRING", "date": "DATE"})

# Stamped on every item this adapter writes, so the item says who maintains it.
# The query worth having is the inverse: an item in these vaults *without* this
# tag is one a person put there and nothing keeps up to date.
#
# A flat adjective rather than the `severino-hq.role=controller` namespace the
# containers use. That convention is a key and a value on a system that has
# them; a tag is one word, and the useful reading here is a plain claim of
# ownership rather than a role within it.
MANAGED_TAG = "hq-managed"

# The two files the item carries, and the only two it will ever be given. Single
# words on purpose: `op` reads a dot in an attachment label as a section
# separator, so `fullchain.pem` would be stored as a file named `pem` inside a
# section named `fullchain`, and the reference an operator or a reader needs
# would become `op://<vault>/<item>/fullchain/pem` instead of
# `op://<vault>/<item>/fullchain`.
ATTACHMENT_LABELS = ("fullchain", "privkey")

# A reader for the certificate and its key, called only when they are actually
# going to be uploaded. Lazy rather than eager so the ordinary pass -- nothing
# changed, nothing to write -- never reads a private key off disk at all.
Material = Callable[[], tuple[bytes, bytes]]


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


def _as_written(field: Mapping[str, Any]) -> tuple[str, str]:
    """One field's stored type beside its value, in the form it was written in.

    `op` takes a date as `2026-10-23` and stores epoch seconds, so the item reads
    back as `1792731600`, which cannot compare equal to the value that produced
    it. The epoch is midnight on that date in the writing machine's timezone, so
    rendering it in local time is the inverse of the parse. An unparseable value
    is left as it came: that reads as a difference and corrects on the next
    write, rather than raising over a field about to be overwritten anyway.
    """

    kind = str(field.get("type", "") or "")
    value = str(field.get("value", "") or "")
    if kind == "DATE" and value:
        try:
            value = date.fromtimestamp(int(value)).isoformat()
        except (OSError, OverflowError, ValueError):
            pass
    return kind, value


def _token(runtime: ProviderRuntime, connection_ref: str) -> str:
    prefix = runtime.connection_prefix("onepassword", connection_ref)
    return runtime.required(prefix, "API_TOKEN")


def _current(
    runtime: ProviderRuntime, publication: dict[str, Any], token: str
) -> tuple[dict[str, tuple[str, str]], tuple[str, ...], frozenset[str]]:
    """What the item already says, every tag it carries, and which files it holds.

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

    Files come back as names only, never content. Whether the material is current
    is answered by the fingerprint this adapter already publishes, so there is
    never a reason to download a private key to find out.
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
        str(field.get("label", "")): _as_written(field)
        for field in document.get("fields") or ()
        if str(field.get("label", "")) in PUBLISHED_FIELDS
    }
    tags = tuple(str(tag) for tag in document.get("tags") or () if str(tag).strip())
    # A label without a dot is stored as the file's own name and no section, so
    # this is what an `op://<vault>/<item>/<label>` reference resolves against.
    files = frozenset(
        str(entry.get("name", ""))
        for entry in document.get("files") or ()
        if str(entry.get("name", ""))
    )
    return fields, tags, files


def publish(
    runtime: ProviderRuntime,
    publication: dict[str, Any],
    desired: dict[str, str],
    material: Material | None = None,
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

    The certificate itself is written too, as the two attachments named in
    ``ATTACHMENT_LABELS``. Facts without material would be the worse of the two
    halves: on renewal the expiry and fingerprint here would move while the files
    stayed at whatever was last put there by hand, and the item would state a
    date above material that contradicts it. Either this owns the whole item or
    it should not own part of one.

    Whether the material is current is answered by the fingerprint, which is the
    certificate's own content identity and is already being published. So the
    key is never read back out of 1Password to compare, and on the ordinary pass
    it is not read off disk either -- ``material`` is called only once something
    has actually changed.

    Two things it does not do, each deliberate:

    * No deletes. Not a field, not the item, not a file. It writes the labels
      above and leaves every other field, and the item itself, as it found them.
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
    current, tags, files = _current(runtime, publication, token)
    changed = sorted(
        label
        for label, kind in PUBLISHED_FIELDS.items()
        if current.get(label) != (_STORED_AS[kind], desired[label])
    )
    tagged = MANAGED_TAG in tags
    # The published fingerprint is the leaf's content identity. If the item
    # already carries this one and both files are there, what is attached is this
    # certificate, and uploading it again would write the same bytes.
    fingerprint_moved = FINGERPRINT_LABEL in changed
    missing = [label for label in ATTACHMENT_LABELS if label not in files]
    send_material = material is not None and (fingerprint_moved or missing)

    if not changed and tagged and not send_material:
        return {
            "target": publication["name"],
            "written": False,
            "fields": [],
            "tagged": True,
            "material": "current",
        }

    assignments = [
        # Assignments only, one per label this adapter owns, each carrying the
        # type it is stored as. `op` treats an assignment as an upsert of that
        # one field, so nothing else on the item is named and nothing can be
        # removed by what is left out.
        f"{label}[{PUBLISHED_FIELDS[label]}]={desired[label]}"
        for label in changed
    ]
    staged = tempfile.mkdtemp(prefix="hq-tls-") if send_material else None
    try:
        if staged is not None and material is not None:
            os.chmod(staged, 0o700)
            for label, content in zip(ATTACHMENT_LABELS, material(), strict=True):
                path = Path(staged) / label
                # Opened rather than written, so the mode is set by the syscall
                # that creates the file instead of a moment afterwards.
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
                assignments.append(f"{label}[file]={path}")
        runtime.run(
            [
                "op",
                "item",
                "edit",
                publication["item"],
                "--vault",
                publication["vault"],
                # The union, never just this adapter's tag: `--tags` sets the
                # whole list, so writing less than this drops whatever the owner
                # added.
                "--tags",
                ",".join(sorted({*tags, MANAGED_TAG})),
                *assignments,
            ],
            env={"OP_SERVICE_ACCOUNT_TOKEN": token},
            step=f"1Password write for {publication['name']}",
        )
    finally:
        if staged is not None:
            shutil.rmtree(staged, ignore_errors=True)
    return {
        "target": publication["name"],
        "written": True,
        "fields": changed,
        "tagged": True,
        # A word, never the labels or anything shaped like a path to a key.
        "material": "written" if send_material else "current",
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

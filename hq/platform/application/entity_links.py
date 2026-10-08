"""One way to name a thing on a page: its label, and where it goes.

``entity_link(kind, identity)`` answers for three registries. A node kind
declares its HQ page here. A reading kind (``control_plane.observations``) and
a resource kind (``control_plane.providers``) declare an optional provider
console link, built only from ids a record stores; such a link is external.
Every entity name a page renders comes through here, so a name either links
the same way everywhere or nowhere.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit

from hq.platform.application.routes import reverse

from hq.domains.control_plane.names import normalized_hostname, names_a_host
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.providers import PROVIDERS, registry_label


@dataclass(frozen=True)
class EntityLink:
    """A name, and the page it opens, when there is one."""

    label: str
    url: str = ""
    external: bool = False
    # The registry kind, and what it is called in a sentence.
    kind: str = ""
    kind_label: str = ""
    # Shown on hover: the other names one link stands for.
    title: str = ""
    # What a reading's record is, in a few words, beside its name.
    detail: str = ""
    # What the kind's page is addressed by: a record's key, a machine's name.
    # The label is what a person reads, so nothing matches on it.
    identity: str = ""

    def __str__(self) -> str:
        return self.label


@dataclass(frozen=True)
class NodeKind:
    """A kind of entity HQ names: what it is called, and its HQ page if it has one."""

    noun: str
    plural: str
    page: Callable[[str], str] | None = None


def connection_anchor(ref: str) -> str:
    """The id of a connection's row on the connections page."""

    slug = "".join(char if char.isalnum() else "-" for char in str(ref).lower())
    return f"connection-{slug.strip('-')}"


def _connection_page(ref: str) -> str:
    return f"{reverse('control_plane:connections')}#{connection_anchor(ref)}"


def _service_page(hostname: str) -> str:
    # A wildcard or a metadata name (``_dmarc``, ``sig1._domainkey``) is not a host.
    if "*" in hostname or not names_a_host(hostname):
        return ""
    return reverse("control_plane:service", kwargs={"hostname": normalized_hostname(hostname)})


NODE_KINDS: Mapping[str, NodeKind] = {
    "controller": NodeKind("controller", "controllers"),
    "connection": NodeKind("connection", "connections", _connection_page),
    "machine": NodeKind(
        "machine",
        "machines",
        lambda name: reverse("control_plane:machine", kwargs={"name": name}),
    ),
    "service": NodeKind("service", "services", _service_page),
    "zone": NodeKind(
        "domain", "domains", lambda zone: reverse("zones:detail", kwargs={"zone": zone})
    ),
    "ability": NodeKind("reading", "readings"),
    "resource": NodeKind(
        "record",
        "records",
        lambda key: reverse("control_plane:detail", kwargs={"key": key}),
    ),
    "registry": NodeKind("lookup", "lookups"),
    # Not a topology node; named on pages all the same.
    "project": NodeKind(
        "project", "projects", lambda slug: reverse("projects:detail", kwargs={"slug": slug})
    ),
    # The records: each names its own page, so a record's name is a link wherever it is said.
    "asset": NodeKind("asset", "assets", lambda slug: reverse("assets:detail", kwargs={"slug": slug})),
    "writeup": NodeKind(
        "writeup or page", "writeups and pages", lambda slug: reverse("content:detail", kwargs={"slug": slug})
    ),
    "document": NodeKind(
        "document", "documents", lambda doc_id: reverse("docs_index:detail", kwargs={"doc_id": doc_id})
    ),
    "expense": NodeKind("expense", "expenses", lambda pk: reverse("expenses:detail", kwargs={"pk": pk})),
    "receipt": NodeKind("receipt", "receipts", lambda pk: reverse("receipts:detail", kwargs={"pk": pk})),
    "target": NodeKind("account or item", "accounts and items"),
    "dependency": NodeKind("not in HQ", "not in HQ"),
}


def record_name(kind: str, spec: Mapping[str, Any], key: str = "") -> str:
    """The name a record stands for, from its settings; its key when it has none.

    A key is HQ's filing of a record. What a person knows it by is the name it
    answers for and where that leads, or the name the thing itself carries. A
    kind that is called something else says so (``ProviderSpec.name``). A
    certificate keeps its key: it covers many names and is known by its own.
    """

    provider = PROVIDERS.get(kind)
    if provider is None:
        return key
    try:
        if provider.name is not None:
            return str(provider.name(dict(spec)) or key)
        names = provider.hostnames(dict(spec)) if provider.hostnames and not provider.covers else ()
        if names:
            leads_to = provider.origin(dict(spec)) if provider.origin else ""
            return f"{names[0]} → {leads_to}" if leads_to else str(names[0])
    except (KeyError, TypeError, ValueError):
        return key
    return key if provider.covers else str(spec.get("name") or key)


def record_names() -> Mapping[str, str]:
    """Every tracked record's name by its key, from the read a page already shares.

    Built from ``infrastructure.enabled_resources``, which every page that
    names a record has read, so naming one costs no query of its own. A record
    switched off in HQ is not in that read and keeps its key here; the pages
    that list those pass the name themselves.
    """

    from .infrastructure import enabled_resources
    from .projection import read_once

    return read_once(
        "entity_links.record_names",
        lambda: {
            resource.key: record_name(resource.kind, resource.spec or {}, resource.key)
            for resource in enabled_resources()
        },
    )


def node_name(node: Any) -> str:
    """What a page calls a topology node: a record by the name it stands for."""

    return str(getattr(node, "name", "") or node.label)


def _record_label(key: str) -> str:
    return record_names().get(key, key) if key else key


def kind_label(kind: str) -> str:
    """What a kind is called in a sentence. Never the identifier itself."""

    if kind in NODE_KINDS:
        return NODE_KINDS[kind].noun.capitalize()
    return registry_label(kind)


def web_url(value: Any) -> str:
    """``value`` when it is an http(s) URL naming a host, else "".

    What an href may be when someone else wrote it: a reading, an attestation,
    a manifest, a form on the public site. A ``javascript:`` or ``data:`` link
    on an operator's page is a phishing line at best, so it is not one.
    """

    text = str(value or "").strip()
    parts = urlsplit(text)
    return text if parts.scheme in ("http", "https") and parts.hostname else ""


def entity_link(
    kind: str,
    identity: str,
    *,
    record: Mapping[str, Any] | None = None,
    label: str = "",
) -> EntityLink:
    """The link for one thing of ``kind``.

    ``identity`` is what the kind's page is addressed by: a machine's name, a
    hostname, a domain, a connection ref, a declaration key. A reading or a
    swept record passes ``record``; its label defaults to the reading's title
    and its link is the provider console, when the record holds the ids.
    Raises ``KeyError`` for a kind no registry knows.
    """

    identity = str(identity or "")
    node = NODE_KINDS.get(kind)
    if node is not None:
        url = node.page(identity) if node.page and identity else ""
        named = _record_label(identity) if kind == "resource" else identity
        return EntityLink(
            label=label or named,
            url=url,
            kind=kind,
            kind_label=kind_label(kind),
            title=identity if named != identity else "",
            identity=identity,
        )
    spec = OBSERVATIONS.get(kind)
    if spec is not None:
        record = record or {}
        url = web_url(spec.console(record)) if record else ""
        return EntityLink(
            label=label or spec.title(record) or identity or spec.label,
            url=url,
            external=bool(url),
            kind=kind,
            kind_label=spec.label,
            detail=spec.describe(record) if record else "",
        )
    provider = PROVIDERS.get(kind)
    if provider is not None:
        if record is None:
            # A declaration: its page in HQ.
            named = _record_label(identity)
            return EntityLink(
                label=label or named,
                url=NODE_KINDS["resource"].page(identity) if identity else "",
                kind=kind,
                kind_label=kind_label(kind),
                title=identity if named != identity else "",
                identity=identity,
            )
        url = web_url(provider.console(record)) if provider.console else ""
        return EntityLink(
            label=label or identity,
            url=url,
            external=bool(url),
            kind=kind,
            kind_label=kind_label(kind),
        )
    raise KeyError(f"No registry declares the kind {kind!r}.")


def container_link(host: str, name: str, watcher: str = "") -> EntityLink:
    """A container by its name: its own page when a record tracks it
    (``watcher``), else its row on its machine's page, which every container
    has. No link when neither is known."""

    from django.utils.text import slugify

    if watcher:
        return entity_link("resource", watcher, label=name)
    page = NODE_KINDS["machine"].page(host) if host and name else ""
    return EntityLink(
        label=name,
        url=f"{page}#container-{slugify(name)}" if page else "",
        kind="resource",
        kind_label="Container",
    )


def declared_link(label: str, url: str = "", kind: str = "target") -> EntityLink:
    """A name an emitter declared with its own url: a connection's target.

    The url is kept, marked external when it leaves HQ.
    """

    url = str(url or "")
    # A page in HQ stays as it is; anything else must be a web address.
    internal = url.startswith("/") and not url.startswith("//")
    url = url if internal else web_url(url)
    return EntityLink(
        label=label,
        url=url,
        external=bool(url) and not internal,
        kind=kind,
        kind_label=kind_label(kind),
    )


def node_link(node: Any) -> EntityLink:
    """The link for a topology node.

    A node carries the url its builder gave it: an estate node's comes from
    ``entity_link``, a declaration's from its provider's ``home``. A node kind
    with an HQ page and no url gets that page; a url that leaves HQ is marked
    external.
    """

    kind = NODE_KINDS.get(node.kind)
    url = str(getattr(node, "url", "") or "")
    if not url and kind is not None and kind.page is not None and node.label:
        url = kind.page(node.label)
    # A declaration is named by its provider's label, not "Resource".
    registry_kind = str(getattr(node, "kind_key", "") or "")
    named = registry_kind if registry_kind in PROVIDERS or registry_kind in OBSERVATIONS else node.kind
    # A declaration's node is labelled by its key, which is what a command
    # is addressed by. A page names it by what it stands for.
    shown = node_name(node)
    return EntityLink(
        label=shown,
        url=url,
        external=url.startswith(("http://", "https://")),
        kind=node.kind,
        kind_label=kind_label(named),
        title=node.label if shown != node.label else "",
        identity=node.label,
    )

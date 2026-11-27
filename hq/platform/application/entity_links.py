"""One way to name a thing on a page: its label, and where it goes.

Every entity name a page renders comes through ``entity_link(kind, identity)``,
so a name links the same way everywhere or nowhere. A node kind declares its HQ
page here; a reading or resource kind may declare an external console link,
built only from ids a record stores.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Protocol
from urllib.parse import urlsplit

lazy from django.utils.text import capfirst, slugify

from hq.domains.control_plane.names import LEADS_TO, names_a_host, normalized_hostname
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.providers import PROVIDERS, read_spec, registry_label
from hq.platform.application.routes import reverse
lazy from hq.domains.control_plane.connection_kinds import CONNECTION_LABELS

from .command_fields import Describe
from .labels import human_label
from .references import Kind
lazy from .connections import connection_names
lazy from .infrastructure import enabled_resources
lazy from .outbound_work import job_names, reader_of
lazy from .projection import read_once


@dataclass(frozen=True, slots=True)
class EntityLink:
    """A name, and the page it opens, when there is one."""

    label: Annotated[str, Describe("The name shown; never matched on, since `identity` is what addresses it.")]
    url: Annotated[str, Describe("The page the name opens: a path in HQ, or an outside address. Empty for none.")] = ""
    external: Annotated[bool, Describe("Whether `url` leaves HQ, such as for a service's console.")] = False
    kind: Annotated[
        str, Describe("The type of the thing named, such as `machine` or a record's type in the registry.")
    ] = ""
    kind_label: Annotated[str, Describe("What that type is called in a sentence.")] = ""
    title: Annotated[str, Describe("The other names this one link stands for, shown on hover. Empty for none.")] = ""
    detail: Annotated[str, Describe("What a reading's record is, in a few words beside its name.")] = ""
    identity: Annotated[
        str,
        Describe(
            "What the type's page is addressed by, and what to match on: a record's key, a machine's name. "
            "Empty for a reading's record."
        ),
    ] = ""

    def __str__(self) -> str:
        return self.label


@dataclass(frozen=True, slots=True)
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
    Kind.CONTROLLER: NodeKind("controller", "controllers"),
    Kind.CONNECTION: NodeKind("connection", "connections", _connection_page),
    Kind.MACHINE: NodeKind(
        "machine",
        "machines",
        lambda name: reverse("control_plane:machine", kwargs={"name": name}),
    ),
    Kind.SERVICE: NodeKind("service", "services", _service_page),
    Kind.ZONE: NodeKind("domain", "domains", lambda zone: reverse("zones:detail", kwargs={"zone": zone})),
    Kind.ABILITY: NodeKind("reading", "readings"),
    Kind.RESOURCE: NodeKind(
        "record",
        "records",
        lambda key: reverse("control_plane:detail", kwargs={"key": key}),
    ),
    Kind.REGISTRY: NodeKind("lookup", "lookups"),
    # Not a topology node; named on pages all the same.
    Kind.PROJECT: NodeKind("project", "projects", lambda slug: reverse("projects:detail", kwargs={"slug": slug})),
    # The records: each names its own page, so a record's name is a link wherever it is said.
    Kind.ASSET: NodeKind("asset", "assets", lambda slug: reverse("assets:detail", kwargs={"slug": slug})),
    Kind.WRITEUP: NodeKind(
        "writeup or page", "writeups and pages", lambda slug: reverse("content:detail", kwargs={"slug": slug})
    ),
    Kind.DOCUMENT: NodeKind(
        "document", "documents", lambda doc_id: reverse("docs_index:detail", kwargs={"doc_id": doc_id})
    ),
    Kind.EXPENSE: NodeKind("expense", "expenses", lambda pk: reverse("expenses:detail", kwargs={"pk": pk})),
    Kind.RECEIPT: NodeKind("receipt", "receipts", lambda pk: reverse("receipts:detail", kwargs={"pk": pk})),
    Kind.MESSAGE: NodeKind("message", "messages", lambda pk: reverse("contacts:detail", kwargs={"pk": pk})),
    Kind.RUN: NodeKind("run", "runs", lambda pk: reverse("jobs:run", kwargs={"pk": pk})),
    Kind.TARGET: NodeKind("account or item", "accounts and items"),
    Kind.DEPENDENCY: NodeKind("not in HQ", "not in HQ"),
}


def page_url(kind: str, identity: str) -> str:
    """The HQ page of the thing of ``kind`` named ``identity``, as ``NODE_KINDS``
    declares it; "" for a kind with none. Raises ``KeyError`` for a kind it lacks."""

    node = NODE_KINDS[kind]
    return node.page(identity) if node.page and identity else ""


def record_name(kind: str, spec: Mapping[str, Any], key: str = "", *, leads: bool = True) -> str:
    """The name a record stands for, from its settings; its key when it has none.

    A key is HQ's filing; a person knows a record by the name it answers for. A
    certificate keeps its key, since it covers many names. ``leads=False``
    leaves out where the name leads.
    """

    provider = PROVIDERS.get(kind)
    if provider is None:
        return key

    def named(spec: dict[str, Any]) -> str:
        if provider.name is not None:
            return str(provider.name(spec) or key)
        names = provider.hostnames(spec) if provider.hostnames and not provider.covers else ()
        if names:
            leads_to = provider.origin(spec) if provider.origin and leads else ""
            return f"{names[0]}{LEADS_TO}{leads_to}" if leads_to else str(names[0])
        return ""

    found = read_spec(kind, spec, named)
    if found is None:
        return key
    if found:
        return found
    return key if provider.covers else str(spec.get("name") or key)


def record_names() -> Mapping[str, str]:
    """Every tracked record's name by its key, from the read a page already shares.

    Built from ``infrastructure.enabled_resources``, which every page that
    names a record has read, so naming one costs no query of its own. A record
    switched off in HQ is not in that read and keeps its key here; the pages
    that list those pass the name themselves.
    """

    return read_once(
        "entity_links.record_names",
        lambda: {
            resource.key: record_name(resource.kind, resource.spec or {}, resource.key)
            for resource in enabled_resources()
        },
    )


def node_name(node: Any) -> str:
    """What a page calls a topology node: a record by the name it stands for."""

    return node.name or node.label


def _record_label(key: str) -> str:
    return record_names().get(key, key) if key else key


def _connection_label(ref: str) -> str:
    return connection_names().get(ref, ref) if ref else ref


# A kind whose identity is HQ's filing of it, by what a person calls it instead.
_NAMED: Mapping[str, Callable[[str], str]] = {"resource": _record_label, "connection": _connection_label}


def kind_label(kind: str) -> str:
    """What a kind is called in a sentence. Never the identifier itself."""

    if kind in NODE_KINDS:
        return NODE_KINDS[kind].noun.capitalize()
    # A reading only HQ's own work takes is called what that work is.
    if kind not in PROVIDERS and kind not in OBSERVATIONS and (reader := reader_of(kind)):
        return job_names()[reader]
    return registry_label(kind)


def provider_label(provider: str) -> str:
    """What a provider is called: its connection's label, else its name in words ("host" is "Host")."""

    return CONNECTION_LABELS.get(provider, human_label(provider))


def web_url(value: Any) -> str:
    """``value`` when it is an http(s) URL naming a host, else "".

    What an href may be when someone else wrote it: a reading, an attestation,
    a manifest, a form on the public site. A ``javascript:`` or ``data:`` link
    on an operator's page is a phishing line at best, so it is not one.
    """

    text = _as_read(value)
    try:
        parts = urlsplit(text)
    except ValueError:  # a bracketed host that is no IPv6 address
        return ""
    return text if parts.scheme in ("http", "https") and parts.hostname else ""


def https_url(value: Any) -> str:
    """``value`` when it is an https URL naming a host, else "": a ``web_url``
    HQ may also send a request to, or a credential over."""

    text = web_url(value)
    return text if urlsplit(text).scheme == "https" else ""


def link_url(value: Any) -> str:
    """``value`` when it is a page in HQ (a path) or a ``web_url``, else ""."""

    text = _as_read(value)
    # A path that starts with two slashes, as a browser reads it, names
    # another host.
    if text.startswith("/") and not text.startswith("//"):
        return text
    return web_url(text)


def _as_read(value: Any) -> str:
    """``value`` as a browser reads it in an href, or "" when the two would differ."""

    text = str(value or "").strip()
    # A browser drops control characters Python's parser keeps, so the two
    # would disagree about what the link names.
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in text):
        return ""
    # A browser reads a backslash in an http(s) address as a slash, so the
    # href is written as the browser reads it, never as a second spelling.
    return text.replace("\\", "/")


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
        named = _NAMED.get(kind, str)(identity)
        return EntityLink(
            label=label or named,
            url=page_url(kind, identity),
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
                url=page_url(Kind.RESOURCE, identity),
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

    if watcher:
        return entity_link("resource", watcher, label=name)
    page = page_url(Kind.MACHINE, host) if name else ""
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

    url = link_url(url)
    return EntityLink(
        label=label,
        url=url,
        external=bool(web_url(url)),
        kind=kind,
        kind_label=kind_label(kind),
    )


class Addressed(Protocol):
    """A record with a page of its own, as Django declares one."""

    def get_absolute_url(self) -> str:
        """The address of the record's own page."""


def record_link(row: Addressed, section: str = "") -> EntityLink:
    """A record by its own page, the address its model declares, so no page
    spells the record's route and key. ``section`` is the part of the page
    the link opens at (``updates`` for ``#updates``)."""

    url = row.get_absolute_url()
    meta = getattr(row, "_meta", None)
    return EntityLink(
        label=str(row),
        url=f"{url}#{section}" if section else url,
        kind=meta.label_lower if meta else "",
        kind_label=capfirst(meta.verbose_name) if meta else "",
        identity=str(getattr(row, "pk", "") or ""),
    )


def node_link(node: Any) -> EntityLink:
    """The link for a topology node.

    A node carries the url its builder gave it: an estate node's comes from
    ``entity_link``, a declaration's from its provider's ``home``. A node kind
    with an HQ page and no url gets that page; a url that leaves HQ is marked
    external.
    """

    url = node.url or (page_url(node.kind, node.label) if node.kind in NODE_KINDS else "")
    # A declaration is named by its provider's label, not "Resource".
    registry_kind = node.kind_key
    named = registry_kind if registry_kind in PROVIDERS or registry_kind in OBSERVATIONS else node.kind
    # A declaration's node is labeled by its key, which is what a command
    # is addressed by. A page names it by what it stands for.
    shown = node_name(node)
    return EntityLink(
        label=shown,
        url=url,
        external=bool(web_url(url)),
        kind=node.kind,
        kind_label=kind_label(named),
        title=node.label if shown != node.label else "",
        identity=node.label,
    )

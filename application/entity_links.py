"""One way to name a thing on a page: its label, and where it goes.

``entity_link(kind, identity)`` answers for three registries. A node kind
declares its HQ page here. A reading kind (``control_plane.observations``) and
a resource kind (``control_plane.providers``) declare an optional provider
console link, built only from ids a record stores; such a link is external.
Every entity name a page renders comes through here, so a name either links
the same way everywhere or nowhere.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

from django.urls import reverse

from control_plane.names import normalized_hostname
from control_plane.observations import OBSERVATIONS
from control_plane.providers import PROVIDERS, names_a_host, registry_label


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
    "ability": NodeKind("ability", "abilities"),
    "resource": NodeKind(
        "resource",
        "resources",
        lambda key: reverse("control_plane:detail", kwargs={"key": key}),
    ),
    "registry": NodeKind("public registry", "public registries"),
    # Not a topology node; named on pages all the same.
    "project": NodeKind(
        "project", "projects", lambda slug: reverse("projects:detail", kwargs={"slug": slug})
    ),
    "target": NodeKind("target", "targets"),
    "dependency": NodeKind("dependency", "dependencies"),
}


def kind_label(kind: str) -> str:
    """What a kind is called in a sentence. Never the identifier itself."""

    if kind in NODE_KINDS:
        return NODE_KINDS[kind].noun.capitalize()
    return registry_label(kind)


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
        return EntityLink(
            label=label or identity, url=url, kind=kind, kind_label=kind_label(kind)
        )
    spec = OBSERVATIONS.get(kind)
    if spec is not None:
        record = record or {}
        url = spec.console(record) if record else ""
        return EntityLink(
            label=label or spec.title(record) or identity or spec.label,
            url=url,
            external=bool(url),
            kind=kind,
            kind_label=spec.label,
        )
    provider = PROVIDERS.get(kind)
    if provider is not None:
        if record is None:
            # A declaration: its page in HQ.
            return EntityLink(
                label=label or identity,
                url=NODE_KINDS["resource"].page(identity) if identity else "",
                kind=kind,
                kind_label=kind_label(kind),
            )
        url = provider.console(record) if provider.console else ""
        return EntityLink(
            label=label or identity,
            url=url,
            external=bool(url),
            kind=kind,
            kind_label=kind_label(kind),
        )
    raise KeyError(f"No registry declares the kind {kind!r}.")


def declared_link(label: str, url: str = "", kind: str = "target") -> EntityLink:
    """A name an emitter declared with its own url: a connection's target.

    The url is kept, marked external when it leaves HQ.
    """

    url = str(url or "")
    return EntityLink(
        label=label,
        url=url,
        external=url.startswith(("http://", "https://")),
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
    return EntityLink(
        label=node.label,
        url=url,
        external=url.startswith(("http://", "https://")),
        kind=node.kind,
        kind_label=kind_label(named),
    )

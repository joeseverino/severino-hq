"""A project from the side of what serves it, and names in its notes as links.

Nothing points a project at infrastructure. A project that says where it is
published has said which service it is (``published_sites``), and the service
knows the machine and the container that answer for it. Read from that side,
the project's page can say where it runs.

Notes are free text. A machine or a document a note names is a link wherever
the name is one HQ knows, and plain text everywhere else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from urllib.parse import urlparse

from hq.domains.control_plane.names import normalized_hostname
from hq.domains.control_plane.provider_adapters.declarations import MACHINE_KIND

from .entity_links import EntityLink, container_link, entity_link
from .projection import read_once


@dataclass(frozen=True)
class RunsOn:
    """The service a project is published as, and what answers for it."""

    service: EntityLink
    machine: EntityLink | None = None
    container: EntityLink | None = None


def where_it_runs(public_url: str) -> RunsOn | None:
    """Where the thing published at ``public_url`` runs; None when no service
    HQ knows answers at that name."""

    from .services import alias_target, service_catalog

    hostname = normalized_hostname(urlparse(str(public_url or "")).hostname or "")
    if not hostname:
        return None
    wanted = alias_target(hostname) or hostname
    service = next((found for found in service_catalog() if found.hostname == wanted), None)
    if service is None:
        return None
    container = service.container
    machine = (container.host if container else "") or service.path.machine
    return RunsOn(
        service=entity_link("service", service.hostname),
        machine=entity_link("machine", machine) if machine else None,
        container=(
            container_link(container.host, container.name, container.watcher) if container else None
        ),
    )


def _named() -> dict[str, EntityLink]:
    """Every name a note can link, by the name as written, lower case."""

    def load() -> dict[str, EntityLink]:
        from hq.domains.docs_index.models import DocumentationRecord

        from .infrastructure import enabled_resources

        found: dict[str, EntityLink] = {}
        for title, doc_id in DocumentationRecord.objects.values_list("title", "doc_id"):
            found[str(title).casefold()] = entity_link("document", doc_id, label=title)
        for resource in enabled_resources():
            name = str(resource.spec.get("name") or "") if resource.kind == MACHINE_KIND else ""
            if name:
                found[name.casefold()] = entity_link("machine", name)
        return found

    return read_once("where_it_runs.named", load)


def named_links(text: str) -> tuple[EntityLink | str, ...]:
    """``text`` in order, as plain stretches and the links of the names in it.

    A name is matched whole, whatever its case, and the longest name wins
    where one contains another.
    """

    text = str(text or "")
    named = {name: link for name, link in _named().items() if len(name) > 2} if text.strip() else {}
    if not named:
        return (text,) if text else ()
    pattern = re.compile(
        r"(?<![\w-])(" + "|".join(re.escape(name) for name in sorted(named, key=len, reverse=True)) + r")(?![\w-])",
        re.IGNORECASE,
    )
    parts: list[EntityLink | str] = []
    at = 0
    for match in pattern.finditer(text):
        if match.start() > at:
            parts.append(text[at : match.start()])
        # As the note wrote it, leading where the name leads.
        parts.append(replace(named[match.group(1).casefold()], label=match.group(1)))
        at = match.end()
    if at < len(text):
        parts.append(text[at:])
    return tuple(parts)

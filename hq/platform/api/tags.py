"""The API document's sections: each domain's tag under its nav group, in the bar's order."""

import re
import unicodedata
from collections.abc import Callable, Iterable
from typing import Any

from hq.platform.application.domains import NAVIGATION_GROUPS, Domain, all_domains, domain_navigation
from hq.platform.application.plugins import NavigationItem

from .fragments import camel

# The tag for what belongs to the API itself rather than to one domain: the
# domain whose page this document is drawn on, so it stands where the bar has it.
API_TAG = "hq.api"
# What a nav group's own tag is named under, where no domain heads the group.
_GROUP_PREFIX = "nav."


def _declared_resources(domain: Domain) -> list[str]:
    """The resources a domain declares: its integration's, then its records'."""

    integration = domain.integration
    names = [spec.name for spec in integration.resources()] if integration.resources else []
    return [*names, domain.records.resource] if domain.records else names


def _declared_capabilities(domain: Domain) -> list[str]:
    """The capabilities a domain declares: its integration's commands, and its
    outbound work, each piece asked for through the capability of its name."""

    integration = domain.integration
    commands = [spec.name for spec in integration.capabilities()] if integration.capabilities else []
    return [*commands, *(work.name for work in integration.outbound())] if integration.outbound else commands


def _bar(item: NavigationItem) -> tuple[int, str]:
    return item.order, item.label


def _first_declarer(domains: Iterable[Domain], declared: Callable[[Domain], list[str]]) -> dict[str, str]:
    """Each name some domain declares, owned by the first domain to declare it."""

    owner: dict[str, str] = {}
    for domain in domains:
        for name in declared(domain):
            owner.setdefault(name, domain.id)
    return owner


class Tags:
    """The document's sections: the domains of the domain registry under their nav groups, in the bar's order."""

    def __init__(self, resources: dict[str, Any], capabilities: dict[str, Any]) -> None:
        self.domains = {domain.id: domain for domain in all_domains()}
        domains = self.domains.values()
        # What a domain serves stands in its section by one rule, the host's
        # core and every extension alike: the domain that declares it, else the
        # domain whose nav item is its page (a resource's web page, a
        # capability's starting page, a route's `page`), else, for a
        # capability, the domain of the resource it acts on. What none places
        # carries no tag; only the API's own routes, which serve every
        # domain, stand in its section.
        self.pages = _first_declarer(domains, lambda domain: [item.route for item in domain.navigation])
        self.resource_owner = _first_declarer(domains, _declared_resources)
        for name, resource in resources.items():
            if name not in self.resource_owner and resource.web_route in self.pages:
                self.resource_owner[name] = self.pages[resource.web_route]
        declared = _first_declarer(domains, _declared_capabilities)
        self.capability_owner = {
            name: owner
            for name, capability in capabilities.items()
            if (
                owner := declared.get(name)
                or self.pages.get(capability.reached_from)
                or self.resource_owner.get(capability.subject_resource or "")
            )
        }
        self.placed = {item.route: item for item in reversed(domain_navigation())}
        self.used: set[str] = set()

    def of_resource(self, name: str) -> str | None:
        return self.use(self.resource_owner.get(name))

    def of_capability(self, name: str) -> str | None:
        return self.use(self.capability_owner.get(name))

    def of_route(self, page: str) -> str | None:
        """A route's section: the domain of the page it serves, or the API's own for a route serving every domain."""

        return self.use(self.pages.get(page) if page else API_TAG)

    def use(self, tag: str | None) -> str | None:
        if tag:
            self.used.add(tag)
        return tag

    def _item(self, domain: Domain) -> NavigationItem | None:
        """A domain's first nav item where the bar draws it: an extension may
        place a host page in a group of its own (``domain_navigation``)."""

        placed = sorted((self.placed.get(item.route, item) for item in domain.navigation), key=_bar)
        return placed[0] if placed else None

    def _group(self, domain: Domain) -> str:
        item = self._item(domain)
        return item.group if item else ""

    def _place(self) -> Callable[[Domain], tuple[bool, bool, int, str]]:
        """Where a domain stands: the API's own domain is where a client starts,
        so it and its group lead; the rest keep the bar's order."""

        api = self.domains.get(API_TAG)
        leading = self._group(api) if api else ""

        def place(domain: Domain) -> tuple[bool, bool, int, str]:
            first = domain.id == API_TAG or (bool(leading) and self._group(domain) == leading)
            item = self._item(domain)
            return (not first, domain.id != API_TAG, item.order if item else domain.bar_order, domain.label)

        return place

    def _groups(self, place: Callable[[Domain], Any]) -> dict[str, dict[str, Any]]:
        """Each nav group's tag, by the group's name.

        A group says what it is for as the host or the extension that adds it
        declares. A domain named as its group is that group's section, so no
        section reads "Projects > Projects": it heads the group, its siblings
        under it.
        """

        purposes = {group.name: group.description for domain in self.domains.values() for group in domain.groups}
        purposes.update(NAVIGATION_GROUPS)
        groups: dict[str, dict[str, Any]] = {}
        for domain in sorted(self.domains.values(), key=place):
            group = self._group(domain)
            heads = domain.label == group
            held = groups.get(group)
            # The first domain named as the group heads it; until one does, the group is its own tag.
            if not group or (held is not None and not (heads and held["name"].startswith(_GROUP_PREFIX))):
                continue
            tag = {"name": domain.id if heads else _GROUP_PREFIX + camel(group), "summary": group, "kind": "nav"}
            if said := purposes.get(group) or (domain.description if heads else ""):
                tag["description"] = said
            groups[group] = tag
        return groups

    def render(self) -> list[dict[str, Any]]:
        place = self._place()
        groups = self._groups(place)
        # A group stands where its first domain does, and a domain outside
        # every group keeps its own place among them.
        tags: list[dict[str, Any]] = []
        for domain in sorted((self.domains[tag] for tag in self.used if tag in self.domains), key=place):
            group = groups.get(self._group(domain))
            if group is not None and group not in tags:
                tags.append(group)
            if group is not None and group["name"] == domain.id:
                continue
            tag: dict[str, Any] = {"name": domain.id, "summary": domain.label, "kind": "nav"}
            if domain.description:
                tag["description"] = domain.description
            if group is not None:
                tag["parent"] = group["name"]
            tags.append(tag)
        for group in groups.values():
            if group["name"].startswith(_GROUP_PREFIX):
                _index(group, [tag for tag in tags if tag.get("parent") == group["name"]])
        return tags


def _index(group: dict[str, Any], held: list[dict[str, Any]]) -> None:
    """End a group's description with a link to each domain it holds.

    A group has no operations of its own, so the viewer's section for it,
    closed, would be its heading alone; the links make it the group's index.
    Each goes to the address the viewer gives a tag's section, `#tag/` and
    the tag's name as its slugger writes it (`AY` in Scalar 1.76.4's
    standalone bundle, line 1102).
    """

    links = "\n".join(f"- [{tag['summary']}](#tag/{_scalar_slug(tag['name'])})" for tag in held)
    group["description"] = "\n\n".join(part for part in (group.get("description", ""), links) if part)


def _scalar_slug(name: str) -> str:
    """A tag's name as the viewer's slugger writes it: lowercased, every
    character but a letter, number, space, `_` or `-` dropped, each run of
    those three one `-`, none at either end."""

    slug = unicodedata.normalize("NFC", name[:255].strip()).lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    return re.sub(r"[\s_-]+", "-", slug).strip("-")

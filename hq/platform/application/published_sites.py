"""The names HQ publishes to the internet, and the projects each one serves."""

from urllib.parse import urlparse

from hq.domains.control_plane.models import ManagedResource
from hq.domains.control_plane.names import names_a_host, normalized_hostname
from hq.domains.control_plane.providers import PROVIDERS
from hq.domains.projects.models import Project

from .entity_links import entity_link
from .projection import read_once


def published_projects() -> dict[str, dict[str, str]]:
    """Hostname to the project that publishes there, for the ones that do.

    An annotation, never a requirement. Most of what an operator runs has no
    repository of its own, and a service that cannot name a project is not
    thereby incomplete.
    """

    return {
        hostname: {
            "name": project.name,
            "slug": project.slug,
            "url": entity_link("project", project.slug).url,
        }
        for hostname, project in projects_by_hostname().items()
    }


def projects_by_hostname() -> dict[str, Project]:
    """The project publishing each name, keyed by that name.

    The one place that decides which project a service belongs to. Nothing
    points a project at infrastructure; a project that says where it is
    published has said which service it is, and reading that twice in two
    modules is two answers to one question.
    """

    return read_once("published_sites.projects_by_hostname", _load_projects_by_hostname)


def _load_projects_by_hostname() -> dict[str, Project]:
    found: dict[str, Project] = {}
    for project in Project.objects.exclude(public_url=""):
        hostname = urlparse(project.public_url).hostname
        if hostname:
            # Most recently updated wins a contested hostname: the model orders
            # by ``-updated_at``, and ``setdefault`` keeps the first. Two
            # projects claiming one name is a data problem, but picking the
            # stalest of them would be a worse answer than picking the freshest.
            found.setdefault(normalized_hostname(hostname), project)
    return found


def public_sites() -> tuple[tuple[str, str, str], ...]:
    """Names HQ publishes to the internet, as (label, sub, url).

    A dashboard link to a site is a site HQ declares a public record for, so
    the list follows what HQ publishes now rather than a template's last edit.
    A name pointed at another one published here is left out (``_aliases``).
    """

    projects = published_projects()
    found: dict[str, str] = {}
    targets: dict[str, str] = {}
    for resource in ManagedResource.objects.filter(enabled=True):
        for hostname, origin in _published_names(resource):
            found.setdefault(hostname, projects.get(hostname, {}).get("name", ""))
            targets.setdefault(hostname, origin)
    aliases = _aliases(found, targets)
    return tuple(
        (hostname, sub, f"https://{hostname}") for hostname, sub in sorted(found.items()) if hostname not in aliases
    )


def _published_names(resource: ManagedResource) -> list[tuple[str, str]]:
    """Each host name a resource publishes, with the name it points at.

    Only a provider whose effect is public and that names hosts publishes, and
    its ``hostnames`` hook names none for a record that carries policy, so a
    DMARC entry never reads as a site. A spec its provider cannot read
    publishes nothing.
    """

    provider = PROVIDERS.get(resource.kind)
    if provider is None or not provider.public_effect or provider.hostnames is None:
        return []
    try:
        names = tuple(provider.hostnames(resource.spec))
        origin = provider.origin(resource.spec) if provider.origin else ""
    except KeyError, TypeError, ValueError:
        return []
    target = normalized_hostname(origin)
    hostnames = (normalized_hostname(name) for name in names)
    # A wildcard is a rule about names, not a name anything answers at.
    return [(hostname, target) for hostname in hostnames if hostname and names_a_host(hostname) and "*" not in hostname]


def _aliases(found: dict[str, str], targets: dict[str, str]) -> set[str]:
    """The names whose target is another name published here.

    The board folds those in, and a list that unfolds them shows one site
    twice. An address with a port is where a name is served, not another name
    for it.
    """

    return {
        hostname
        for hostname, target in targets.items()
        if target and ":" not in target and target != hostname and target in found
    }

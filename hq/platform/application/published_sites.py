"""The names HQ publishes to the internet, and the projects each one serves."""

from urllib.parse import urlparse

from hq.domains.control_plane.models import ManagedResource
from hq.domains.control_plane.names import names_a_host, normalized_hostname
from hq.domains.control_plane.providers import PROVIDERS
from hq.domains.projects.models import Project

from .projection import read_once
from .entity_links import entity_link


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

    A dashboard link to a site is the site HQ already declares a public record
    for, so the list is whatever HQ is currently publishing rather than what it
    was publishing when somebody last edited a template.

    Read through the providers that say their effect is public, and through
    their own ``hostnames`` hook, which returns nothing for the record types
    that carry policy, so a DMARC entry never arrives here looking like a site.
    """

    projects = published_projects()
    found: dict[str, str] = {}
    targets: dict[str, str] = {}
    for resource in ManagedResource.objects.filter(enabled=True):
        provider = PROVIDERS.get(resource.kind)
        if provider is None or not provider.public_effect:
            continue
        if provider.hostnames is None:
            continue
        try:
            names = tuple(provider.hostnames(resource.spec))
            origin = provider.origin(resource.spec) if provider.origin else ""
        except (KeyError, TypeError, ValueError):
            continue
        for name in names:
            hostname = normalized_hostname(name)
            # A wildcard is a rule about names, not a name anything answers at.
            if hostname and names_a_host(hostname) and "*" not in hostname:
                found.setdefault(
                    hostname, projects.get(hostname, {}).get("name", "")
                )
                targets.setdefault(hostname, normalized_hostname(origin))
    # A name whose target is another name here is the same site reached a
    # second way. The board folds those in, and a list that unfolds them shows
    # one site twice. An address with a port is where a name is served, not
    # another name for it.
    aliases = {
        hostname
        for hostname, target in targets.items()
        if target and ":" not in target and target != hostname and target in found
    }
    return tuple(
        (hostname, sub, f"https://{hostname}")
        for hostname, sub in sorted(found.items())
        if hostname not in aliases
    )

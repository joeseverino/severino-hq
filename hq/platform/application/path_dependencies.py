"""What a service depends on along its path, and what losing each dependency would mean."""

from dataclasses import dataclass

from .entity_links import EntityLink
from .path_model import Hop, ServicePath, Source

# What changing or losing a hop does to the name, by step.
_CONSEQUENCES = {
    "dns": "The name stops resolving, or resolves somewhere else.",
    "edge": "If it is switched off, visitors reach the origin directly and Cloudflare's certificate no longer applies.",
    "redirect": "Visitors stop being sent on.",
    "served": "The site stops answering at this name.",
    "ingress": "If it is removed, the name stops reaching the app.",
    "machine": "If it is down, this service is down.",
    # The address a record answers with: move it and the record still points
    # at the old one.
    "network": "If the machine's address changes, this name stops working.",
    "upstream": "If nothing listens there, you get 502 Bad Gateway.",
    "container": "The service stops answering.",
    "hq": "HQ stops answering at this name.",
}
_CERTIFICATE_CONSEQUENCE = "Clients see a certificate error once it expires or stops covering the name."


def consequence_of(hop: Hop) -> str:
    """What changing this hop would do, or "" where nothing depends on it."""

    return _CONSEQUENCES.get(hop.step, "")


@dataclass(frozen=True)
class Dependency:
    """One part a name depends on, and what changing it does."""

    label: str
    name: str
    link: EntityLink | None
    consequence: str
    source: Source | None = None


def depends_on(path: ServicePath) -> tuple[Dependency, ...]:
    """The parts every route of ``path`` passes through, once each."""

    found: dict[tuple[str, str], Dependency] = {}
    # A forward on another port is a second way in, not a part the name needs.
    for route in (route for route in path.routes if route.port is None):
        for hop in route.hops:
            consequence = _CONSEQUENCES.get(hop.step)
            if consequence and hop.name:
                found.setdefault(
                    (hop.step, hop.name),
                    Dependency(hop.label, hop.name, hop.link, consequence, hop.source),
                )
            certificate = hop.certificate
            if certificate is not None and certificate.name:
                found.setdefault(
                    ("certificate", certificate.name),
                    Dependency(
                        f"{certificate.role} certificate",
                        certificate.name,
                        certificate.link,
                        _CERTIFICATE_CONSEQUENCE,
                        certificate.source,
                    ),
                )
    return tuple(found.values())

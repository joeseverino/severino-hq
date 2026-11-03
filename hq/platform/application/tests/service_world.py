"""A declared world of services, and the readings that call one healthy.

Every hostname here is under ``example.com`` or ``example.test``. This repository
is public, and a fixture is documentation whether or not it was meant to be.
"""

from hq.domains.control_plane.models import ManagedResource

APP_HOST = {
    "name": "app-host",
    "role": "Docker host",
    "addresses": ["10.0.0.10"],
}


def declare_world(*, containers: bool = False):
    """The machine these tests forward to, and optionally what answers on it.

    A container sharing its machine's network publishes no ports for a sweep to
    find, so what it answers on is declared. That is the case these fixtures
    exercise: nothing here has ever been swept.
    """

    ManagedResource.objects.create(key="app-host", kind="machine", spec=APP_HOST)
    if not containers:
        return
    for name, port in (("web", 8000), ("cache", 6379)):
        ManagedResource.objects.create(
            key=f"app-host-{name}",
            kind="portainer.container",
            spec={
                "connection_ref": "a-portainer",
                "host": "app-host",
                "name": name,
                "serves_ports": [port],
            },
        )


def healthy(resource: ManagedResource) -> ManagedResource:
    resource.conditions = [{"type": "Ready", "status": True, "reason": "", "message": ""}]
    resource.save(update_fields=["conditions"])
    return resource


def facet(service, facet_id):
    """One facet of a service, by name rather than by position."""

    return next(item for item in service.facets if item.id == facet_id)

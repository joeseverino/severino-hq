"""Every running container, and whether what it runs is current and safe."""

from typing import Any

from hq.platform.application.routes import reverse
from django.views.generic import TemplateView

from hq.platform.application.pages import PageMixin


def container_detail(resource: Any, request: Any) -> dict[str, Any]:
    """A watched container's page: which machine that name is, what it is
    doing and which services reach it (``container_context``), then whether
    what it runs is current and safe, how it is run, and what upgrading it
    would take: the same objects the containers page and the containers and
    upgrades resources serve, so none can disagree."""

    from hq.platform.application.containers import containers, standing_of
    from hq.platform.application.machines import container_context
    from hq.platform.application.upgrades import plan_for, readiness_of

    from .views import returning_to

    found = container_context(resource.spec.get("host", ""), resource.spec.get("name", ""))
    running, machine = found.get("running"), found.get("machine")
    if running is None:
        return found
    host = machine.name if machine is not None else resource.spec.get("host", "")
    item = next(
        (each for each in containers() if each.machine.name == host and each.running.name == running.name),
        None,
    )
    return {
        **found,
        "standing": item.standing if item is not None else standing_of(running.image, host, running.name),
        "item": item,
        "plan": plan_for(item) if item is not None else None,
        "readiness": readiness_of(item) if item is not None and item.standing.repository is None else None,
        # Where the operator names what its image is built from, when the image does not say.
        "edit_url": returning_to(reverse("control_plane:edit", args=[resource.key]), request.get_full_path()),
    }


class ContainerListView(PageMixin, TemplateView):
    """Led by what to act on (``needs_you``): a version with a newer release,
    or a vulnerable one the internet may reach, each once however many
    containers run it. Then every container, one row each."""

    template_name = "control_plane/container_list.html"
    page_title = "Containers"

    def get_context_data(self, **kwargs):
        from hq.platform.application.container_attention import needs_you
        from hq.platform.application.containers import (
            BEHIND,
            CURRENT,
            UP_TO_DATE,
            UPDATE_AVAILABLE,
            VULNERABLE,
            containers,
        )
        from hq.platform.application.upgrades import plan_for

        context = super().get_context_data(**kwargs)
        found = containers()
        groups: dict[str, dict] = {}
        for item in found:
            if item.standing.state in (VULNERABLE, BEHIND):
                groups.setdefault(
                    item.standing.label, {"standing": item.standing, "containers": [], "plan": plan_for(item)}
                )["containers"].append(item)
        read = [item.standing.read_at for item in found if item.standing.read_at]
        needs = [group for group in groups.values() if needs_you(group["standing"], group["containers"])]
        # An image is listed to update when a newer version is published. One
        # with a vulnerability and no newer version is listed only while the
        # internet may reach it.
        update_now = [group for group in needs if group["standing"].newer]
        no_update = [group for group in needs if not group["standing"].newer]
        updates = [item.standing.update_state for item in found]
        context.update(
            containers=found,
            needs=needs,
            need_sections=[
                (heading, listed, sum(len(group["containers"]) for group in listed))
                for heading, listed in (
                    ("Update now", update_now),
                    ("Vulnerable, no update yet, and the internet may reach it", no_update),
                )
                if listed
            ],
            machines=len({item.machine.name for item in found}),
            vulnerable=sum(1 for group in groups.values() if group["standing"].state == VULNERABLE),
            vulnerable_containers=sum(1 for item in found if item.standing.state == VULNERABLE),
            behind=sum(1 for group in groups.values() if group["standing"].state == BEHIND),
            # Only what a registry answered for is current; silence is not.
            current=sum(1 for item in found if item.standing.state == CURRENT),
            # Counted per container and apart from vulnerabilities, so the
            # three add up to the containers listed.
            update_available=updates.count(UPDATE_AVAILABLE),
            up_to_date=updates.count(UP_TO_DATE),
            update_unknown=len(found) - updates.count(UPDATE_AVAILABLE) - updates.count(UP_TO_DATE),
            pinned=sum(1 for item in found if item.standing.pinned),
            # Checked against its source's advisories or its packages against OSV.
            matched=sum(1 for item in found if item.standing.checkable),
            unchecked=sum(1 for item in found if not item.standing.checkable),
            read_at=min(read) if read else None,
        )
        return context

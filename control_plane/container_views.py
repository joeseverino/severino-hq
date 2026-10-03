"""Every running container, and whether what it runs is current and safe."""

from __future__ import annotations

from typing import Any

from django.contrib.auth.mixins import LoginRequiredMixin
from application.routes import reverse
from django.views.generic import TemplateView

from application.pages import PageMixin


def container_detail(resource: Any, request: Any) -> dict[str, Any]:
    """A watched container's page: which machine that name is, what it is
    doing and which services reach it (``container_context``), then whether
    what it runs is current and safe, how it is run, and what upgrading it
    would take: the same objects the containers page and the containers and
    upgrades resources serve, so none can disagree."""

    from application.containers import containers, standing_of
    from application.machines import container_context
    from application.upgrades import plan_for, readiness_of

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


class ContainerListView(PageMixin, LoginRequiredMixin, TemplateView):
    """Led by what needs you (``needs_you``): a version with a known
    vulnerability or a newer release, each once however many containers run
    it. Then every container, one row each."""

    template_name = "control_plane/container_list.html"
    page_title = "Containers"

    def get_context_data(self, **kwargs):
        from application.container_attention import needs_you
        from application.containers import BEHIND, CURRENT, VULNERABLE, containers
        from application.upgrades import plan_for

        context = super().get_context_data(**kwargs)
        found = containers()
        groups: dict[str, dict] = {}
        for item in found:
            if item.standing.state in (VULNERABLE, BEHIND):
                groups.setdefault(
                    item.standing.label, {"standing": item.standing, "containers": [], "plan": plan_for(item)}
                )["containers"].append(item)
        read = [item.standing.read_at for item in found if item.standing.read_at]
        context.update(
            containers=found,
            needs=[group for group in groups.values() if needs_you(group["standing"], group["containers"])],
            machines=len({item.machine.name for item in found}),
            vulnerable=sum(1 for group in groups.values() if group["standing"].state == VULNERABLE),
            behind=sum(1 for group in groups.values() if group["standing"].state == BEHIND),
            # Only what a registry answered for is current; silence is not.
            current=sum(1 for item in found if item.standing.state == CURRENT),
            pinned=sum(1 for item in found if item.standing.pinned),
            # Checked against its source's advisories or its packages against OSV.
            matched=sum(1 for item in found if item.standing.source or item.standing.checked),
            read_at=min(read) if read else None,
        )
        return context

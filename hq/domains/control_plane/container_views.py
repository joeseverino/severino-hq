"""Every running container, and whether what it runs is current and safe."""

from typing import Any, override

from django.views.generic import TemplateView

from hq.platform.application.pages import PageMixin
lazy from hq.platform.application.container_attention import needs_you
lazy from hq.platform.application.containers import BEHIND, VULNERABLE, containers, standing_of, tally
lazy from hq.platform.application.machines import container_context
lazy from hq.platform.application.resource_capabilities import offered_changes
lazy from hq.platform.application.upgrades import plan_for, readiness_of

lazy from .views import returning_to


def container_detail(resource: Any, request: Any) -> dict[str, Any]:
    """A watched container's page: which machine that name is, what it is
    doing and which services reach it (``container_context``), then whether
    what it runs is current and safe, how it is run, and what upgrading it
    would take: the same objects the containers page and the containers and
    upgrades resources serve, so none can disagree."""

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
        "edit_url": (
            returning_to(edit_url, request.get_full_path())
            if (edit_url := offered_changes([resource])[resource.key].edit_url)
            else ""
        ),
    }


class ContainerListView(PageMixin, TemplateView):
    """Led by what to act on (``needs_you``): a version with a newer release,
    or a vulnerable one the internet may reach, each once however many
    containers run it. Then every container, one row each."""

    template_name = "control_plane/container_list.html"
    page_title = "Containers"
    page_reads = ("portainer.container",)

    @override
    def get_context_data(self, **kwargs):
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
        # Each reason is a section of its own, so its heading says what it holds.
        need_sections = [
            {
                "heading": heading,
                "lede": lede,
                "groups": listed,
                "running": sum(len(group["containers"]) for group in listed),
            }
            for heading, lede, listed in (
                ("Update now", "", update_now),
                (
                    "No update yet",
                    "Vulnerable images with no newer release, listed while the internet may reach them.",
                    no_update,
                ),
            )
            if listed
        ]
        context.update(
            containers=found,
            need_sections=need_sections,
            machines=len({item.machine.name for item in found}),
            tally=tally(found),
            read_at=min(read) if read else None,
        )
        return context

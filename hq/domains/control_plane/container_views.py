"""Every running container, and whether what it runs is current and safe."""

from typing import Any, override

from django.shortcuts import redirect
from django.views import View
from django.views.generic import TemplateView

from hq.platform.application.pages import PageMixin
from hq.platform.application.routes import reverse
from hq.platform.application.security import safe_next
from hq.platform.application.web_writes import execute_posted
from hq.platform.core.held_values import format_html
from hq.platform.core.web_refusals import refused
lazy from hq.platform.application.app_update_approval import PLAYBOOKS
lazy from hq.platform.application.app_updates import UpdateKind, managed_by, update_state
lazy from hq.platform.application.container_findings import needs_you
lazy from hq.platform.application.containers import BEHIND, VULNERABLE, containers, standing_of, tally
lazy from hq.platform.application.machines import container_context
lazy from hq.platform.application.resource_capabilities import offered_changes
lazy from hq.platform.application.upgrades import plan_for, readiness_of

from .container_tables import every_container, need_table


def container_detail(resource: Any, request: Any) -> dict[str, Any]:
    """A watched container's page: which machine that name is, what it is
    doing and which services reach it (``container_context``), then whether
    what it runs is current and safe, how it is run, and what upgrading it
    would take: the same objects the containers page and the containers and
    upgrades resources serve, so none can disagree. One shape whether or not
    it runs, each part None or empty when nothing does, so no reader indexes a
    key only a running container has."""

    found = container_context(resource.spec.get("host", ""), resource.spec.get("name", ""))
    running, machine = found.get("running"), found.get("machine")
    if running is None:
        return {
            **found,
            "standing": None,
            "item": None,
            "plan": None,
            "state": None,
            "act": None,
            "held": "",
            "readiness": None,
            "managed": None,
            "edit": None,
        }
    host = machine.name if machine is not None else resource.spec.get("host", "")
    item = next(
        (each for each in containers() if each.machine.name == host and each.running.name == running.name),
        None,
    )
    plan = plan_for(item) if item is not None else None
    state = update_state(item) if item is not None else None
    # A move under way stands with no newer release listed: an update open, or a pin merged it does not run yet.
    if plan is None and state is not None and state.kind not in (UpdateKind.OPEN, UpdateKind.MERGED):
        state = None
    return {
        **found,
        "standing": item.standing if item is not None else standing_of(running.image, host, running.name),
        "item": item,
        "plan": plan,
        "state": state,
        "act": state.action.control(request.get_full_path()) if state is not None and state.action else None,
        "held": _held(state),
        # A build on its host is released by its repository's merges, never by a registry's tags.
        "readiness": readiness_of(item) if item is not None and _pulled(item.standing) else None,
        # The repository whose Compose files build its image on its host.
        "managed": managed_by(item) if item is not None and item.standing.local else None,
        # Where the operator names what its image is built from, when the image does not say.
        "edit": offered_changes([resource])[resource.key].edit(resource.key, next=request.get_full_path()),
    }


def _pulled(standing: Any) -> bool:
    return standing.repository is None and not standing.local


def _held(state: Any) -> str:
    """Why its update offers no press, with the page that adopts the playbook
    linked where that is the step; "" when one is offered, or a step of it already stands."""

    if state is None or not state.reason or state.at_work:
        return ""
    if not state.adopt:
        return state.reason
    before, page, after = state.reason.partition(PLAYBOOKS)
    return format_html('{}<a href="{}">{}</a>{}', before, reverse("control_plane:playbooks"), page, after)


class ContainerListView(PageMixin, TemplateView):
    """Led by what to act on (``needs_you``): a version with an upgrade worth
    making, or a vulnerable one the internet may reach, each once however many
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
        # An image is listed to update when its plan recommends the move
        # (``Plan.recommended``). A vulnerable one with no such move is listed
        # only while the internet may reach it.
        update_now = [group for group in needs if group["plan"] is not None and group["plan"].recommended]
        no_update = [group for group in needs if group not in update_now]
        # Each reason is a section of its own, so its heading says what it holds.
        need_sections = [
            {
                "heading": heading,
                "lede": lede,
                "groups": listed,
                "table": need_table(heading, listed),
                "running": sum(len(group["containers"]) for group in listed),
            }
            for heading, lede, listed in (
                ("Update now", "", update_now),
                (
                    "No update yet",
                    "Vulnerable images no release is known to fix, listed while the internet may reach them.",
                    no_update,
                ),
            )
            if listed
        ]
        context.update(
            containers=found,
            every=every_container(found),
            need_sections=need_sections,
            machines=len({item.machine.name for item in found}),
            tally=tally(found),
            read_at=min(read) if read else None,
        )
        return context


class AppUpdatePressView(View):
    """The one press an app update's state offers (``update_state``), asked
    through the command its route says the view ``runs``."""

    runs: tuple[str, ...] = ()

    def post(self, request, owner, name, **route):
        refused(request, execute_posted(request, self.runs[0], {"repository": f"{owner}/{name}", **route}))
        return redirect(safe_next(request, fallback=reverse("control_plane:containers")))

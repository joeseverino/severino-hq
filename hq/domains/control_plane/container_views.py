"""Every running container, and whether what it runs is current and safe."""

from typing import Any, override

from django.shortcuts import redirect
from django.views import View
from django.views.generic import TemplateView

from hq.domains.control_plane.models import OperationRequest
from hq.platform.application.pages import PageAction, PageMixin
from hq.platform.application.resource_operations import ACTION_CAPABILITY
from hq.platform.application.routes import reverse
from hq.platform.application.security import safe_next
from hq.platform.application.web_writes import execute_posted
from hq.platform.core.held_values import format_html
from hq.platform.core.web_refusals import refused
lazy from hq.platform.application.app_update_approval import (
    PLAYBOOKS,
    Approval,
    approval_of,
    approve_target,
    run_again_action,
    unapplied,
)
lazy from hq.platform.application.app_updates import managed_by, update_of
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
            "update": None,
            "approval": None,
            "approve": None,
            "again": None,
            "held": "",
            "readiness": None,
            "edit": None,
        }
    host = machine.name if machine is not None else resource.spec.get("host", "")
    item = next(
        (each for each in containers() if each.machine.name == host and each.running.name == running.name),
        None,
    )
    update = update_of(item) if item is not None else None
    approval = approval_of(update) if update is not None else _closed(item)
    return {
        **found,
        "standing": item.standing if item is not None else standing_of(running.image, host, running.name),
        "item": item,
        "plan": plan_for(item) if item is not None else None,
        "update": update,
        "approval": approval,
        "approve": (
            PageAction.command(
                ACTION_CAPABILITY[OperationRequest.Action.MERGE],
                approve_target(update),
                primary=True,
                query={"next": request.get_full_path()},
            )
            if update is not None and approval is not None and not approval.held
            else None
        ),
        "again": run_again_action(approval.standing) if approval is not None and approval.standing else None,
        "held": _held(approval),
        "readiness": readiness_of(item) if item is not None and item.standing.repository is None else None,
        # Where the operator names what its image is built from, when the image does not say.
        "edit": offered_changes([resource])[resource.key].edit(resource.key, next=request.get_full_path()),
    }


def _closed(item: Any) -> Any:
    """Where no update of ``item`` is open: the approval of its app's newest
    merge while that merge is not applied, else None."""

    managed = managed_by(item) if item is not None else None
    standing = unapplied(managed.repository, managed.app) if managed is not None else None
    return Approval(standing, standing.words) if standing is not None else None


def _held(approval: Any) -> str:
    """Why no approval is offered, with the page that adopts the playbook
    linked where that is the step; "" when one is, or a merge already stands."""

    if approval is None or not approval.held or (approval.standing and approval.standing.taken):
        return ""
    if not approval.adopt:
        return approval.held
    before, page, after = approval.held.partition(PLAYBOOKS)
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


class AppUpdateApproveView(View):
    """One press approving an app update at the head its page showed: the
    merge, and the run that applies it, asked through ``app_update.approve``."""

    runs = (ACTION_CAPABILITY[OperationRequest.Action.MERGE],)

    def post(self, request, owner, name, number, head_sha):
        payload = {"repository": f"{owner}/{name}", "number": number, "head_sha": head_sha}
        refused(request, execute_posted(request, self.runs[0], payload))
        return redirect(safe_next(request, fallback=reverse("control_plane:containers")))

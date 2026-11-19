"""Machines and the tailnet: the list, one machine's page, and what a change to either would reach."""

from functools import cached_property
from typing import override

from django.urls import reverse_lazy
from django.views.generic import TemplateView

from hq.platform.application.connections import machines_once
from hq.platform.application.hq_self import LABEL as HQ_LABEL
from hq.platform.application.machine_context import (
    machine_links,
    problem_parts,
    sections_for as machine_sections,
    serves_links,
)
from hq.platform.application.machines import Machine, machine
from hq.platform.application.pages import PageAction, PageDialog, PageMixin, RecordListMixin, RecordPageMixin
from hq.platform.application.resource_capabilities import (
    CREATE_CAPABILITY,
    MACHINE_DETAILS,
    REMOVAL_CAPABILITY,
    Offered,
    offered_changes,
    resource_capabilities,
)
from hq.platform.application.routes import reverse
from hq.platform.application.security import web_principal
from hq.platform.application.service_context import RUNS_SECTION
from hq.platform.application.tailnet_context import tailnet_context
from hq.platform.application.visit_refresh import page_visit
lazy from hq.platform.application import tailnet
lazy from hq.platform.application.automation_runs import HERE, RUN_HERE, PlaybookHere, playbooks_on
lazy from hq.platform.application.containers import on_machine
lazy from hq.platform.application.docker_sections import docker_line
lazy from hq.platform.application.page_relations import for_machine
lazy from hq.platform.application.problems import grouped, problems_by_part
lazy from hq.platform.application.request_channel import forwarding_of
lazy from hq.platform.application.resource_operations import ACTION_CAPABILITY
lazy from hq.platform.application.tailnet import declaration, may_reach, ports, proposed_grant

from .models import ManagedResource, OperationRequest
from .names import normalized_hostname
from .provider_adapters.declarations import MACHINE_KIND


class MachineListView(RecordListMixin, TemplateView):
    """Every machine anything reported, and what is on each.

    Nothing here is declared. A machine exists because a credential reaches it,
    a container runs on it, or a service is served from it, so adding a VPS is
    registering it somewhere rather than entering it here.
    """

    template_name = "control_plane/machine_list.html"
    page_title = "Machines"
    page_reads = ("tailscale.device", "portainer.environment")
    resource = "machines"
    context_object_name = "machines"
    fields = (
        "name",
        "url",
        "role",
        "roles",
        "state",
        "tone",
        "reached_by",
        "unanswered",
        "containers.name",
        "running",
        "containers_visible",
        "hostnames",
        "runs_hq",
        "presence.online",
        "presence.last_seen",
    )

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["hq_label"] = HQ_LABEL
        # How many are in each state, in the order the states first appear.
        states = [item["state"] for item in self.records]
        context["state_counts"] = [(states.count(state), state) for state in dict.fromkeys(states)]
        return context


def policy_edit(key: str) -> PageAction | None:
    """The tailnet policy's editor, when its declaration offers one."""

    resource = ManagedResource.objects.filter(key=key).first() if key else None
    return offered_changes([resource])[key].edit(key) if resource is not None else None


def _route_approval(device: ManagedResource | None) -> PageAction | None:
    """The press that approves the routes a machine advertises, through its
    tailnet device, unusable with the reason its connection gives."""

    allowed = (
        resource_capabilities(device, running=()).actions.get(OperationRequest.Action.APPROVE_ROUTES)
        if device is not None
        else None
    )
    if allowed is None:
        return None
    refusal = "" if allowed.enabled else allowed.reason
    return PageAction.command(ACTION_CAPABILITY[OperationRequest.Action.APPROVE_ROUTES], device.key, refused=refusal)


def whatif_context(request, default: str = "", *, target_default: str | None = None) -> dict:
    """Everything the reachability panel needs, wherever it is included.

    One function because the panel appears on more than one page and the two
    must not drift into disagreeing about what they were asked. ``default``
    is the device a page is already about, so opening the panel there starts
    with the question most likely being asked.
    """

    known = tailnet.devices()
    asked = {
        "source": request.GET.get("source", "") or default,
        "target": request.GET.get("target", "") or (default if target_default is None else target_default),
        "port": request.GET.get("port", ""),
    }
    context = {
        "device_names": sorted(known),
        "ports": ports(),
        "asked": asked,
        "whatif_action": request.path,
        "policy_edit": policy_edit(declaration()),
    }
    # Answered only when all three were given. A half-filled form is a question
    # nobody has asked yet, not one whose answer is "no".
    if asked["source"] and asked["target"] and asked["port"].isdigit():
        verdict = may_reach(asked["source"], asked["target"], int(asked["port"]))
        context["verdict"] = verdict
        # A refusal is the moment somebody wants to change the policy, so the
        # grant that would allow it is worked out here rather than left as an
        # exercise. Shown, never applied: what to do about a denial is the
        # operator's call and the editor is where it is made.
        if verdict.known and not verdict.allowed:
            context["proposal"] = proposed_grant(asked["source"], asked["target"], int(asked["port"]))
    return context


class TailnetView(PageMixin, TemplateView):
    """The tailnet, and whether one machine may reach another.

    A page because the question has nowhere else to live. Reachability is not a
    property of any one declaration: it is the policy's answer about a pair,
    so it belongs beside the devices rather than on any of them.
    """

    template_name = "control_plane/tailnet.html"
    page_title = "Tailnet"
    page_reads = ("tailscale.policy", "tailscale.device")

    @override
    def get_page_actions(self):
        edit = policy_edit(self.tailnet.declaration)
        return (
            PageAction(
                "Policy test",
                reverse("control_plane:tailnet"),
                primary=True,
                modal=PageDialog(
                    "whatif",
                    "Policy test",
                    "control_plane/_whatif_form.html",
                    lede="Check whether the policy allows a connection. Tailscale evaluates it.",
                ),
            ),
            *((edit,) if edit else ()),
            PageAction("All machines", reverse("control_plane:machines")),
        )

    @override
    def get(self, request, *args, **kwargs):
        self.tailnet = tailnet_context(principal=web_principal(request.user))
        return super().get(request, *args, **kwargs)

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(whatif_context(self.request))
        context["tailnet"] = self.tailnet
        return context


class MachineDetailView(RecordPageMixin, TemplateView):
    """One machine, and everything that ties to it.

    A container's host, a proxy's forwarding address, what a Portainer says it
    reaches and which credential opens a shell there are four facts about one
    thing, and this is the page where they meet.
    """

    template_name = "control_plane/machine_detail.html"
    resource = "machines"
    context_object_name = "machine"
    page_trail = (("Machines", reverse_lazy("control_plane:machines")),)

    @cached_property
    def found(self) -> Machine:
        """The machine as the catalog holds it, which the records of other
        kinds about it are asked by."""

        found = machine(self.record["name"])
        assert found is not None  # noqa: S101 - its record was just read from the same catalog
        return found

    @override
    def get_page_actions(self):
        record = self.record
        if record["declaration"]:
            # The machine's declaration is edited from its own page.
            key = record["declaration"]
            resource = ManagedResource.objects.filter(key=key).first()
            capabilities = resource_capabilities(resource) if resource is not None else None
            offered = capabilities.offered if capabilities is not None else Offered()
            actions = [
                offered.edit(key),
                PageAction.command(REMOVAL_CAPABILITY, key, variant=capabilities.removal_variant)
                if capabilities is not None and offered.removes
                else None,
            ]
        else:
            # Seeded with the name and addresses HQ knows, and back here after
            # saving. A machine the tailnet device declaration already names
            # gets its details added; one with no declaration at all is declared.
            seed = {"name": record["name"], "addresses": record["addresses"]}
            actions = [
                PageAction.command(
                    CREATE_CAPABILITY,
                    variant=MACHINE_DETAILS if record["tailnet_device"] else MACHINE_KIND,
                    query={"kind": MACHINE_KIND, **seed, "next": record["url"]},
                ),
            ]
        return (*self._run_here(), *actions)

    @cached_property
    def playbooks_here(self) -> tuple[PlaybookHere, ...]:
        """Each playbook its reader may run on this machine."""

        return playbooks_on(normalized_hostname(self.record["name"]), web_principal(self.request.user))

    def _run_here(self) -> tuple[PageAction, ...]:
        """One playbook's Run page with this machine chosen, or a dialog of several, Playbooks without script."""

        if not self.playbooks_here:
            return ()
        if len(self.playbooks_here) == 1:
            hosts = (normalized_hostname(self.record["name"]),)
            only = PageAction.command(self.playbooks_here[0].capability, variant=HERE, query={"hosts": hosts})
            return (only,) if only is not None else ()
        dialog = PageDialog("run-playbook", RUN_HERE, "control_plane/_run_playbook.html")
        return (PageAction(RUN_HERE, reverse("control_plane:playbooks"), modal=dialog),)

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        record, found = self.record, self.found
        device = (
            ManagedResource.objects.filter(key=record["tailnet_device"]).first() if record["tailnet_device"] else None
        )
        context["approve_routes"] = _route_approval(device)
        # What else HQ can say about this machine, from a registry rather than
        # from this view. A band appears because a resolver produced one, so
        # what HQ learns next reaches the page without either being edited.
        principal = web_principal(self.request.user)
        sections = machine_sections(found, principal=principal)
        whole, context["relationships"] = for_machine(found, sections, principal=principal)
        context["serves_links"] = serves_links(found, whole)
        # One Docker environment is a line under the containers heading. Its
        # table is left for a machine that has several.
        context["docker"] = docker_line(found) if found.containers else None
        # Its runs are drawn under its load, where an operator looks first.
        context["runs"] = tuple(section for section in sections if section.id == RUNS_SECTION)
        context["sections"] = tuple(
            section
            for section in sections
            if section.id != RUNS_SECTION and not (context["docker"] and section.id == "docker-environment")
        )
        context["playbooks_here"] = self.playbooks_here
        # How many names the page's own table lists, which the band then counts.
        context["names_listed"] = next(
            (len(section.records) for section in context["sections"] if section.id == "names"), 0
        )
        context.update(machine_links(record))
        # Whether you are reading this on the machine it describes: HQ already
        # judged the caller's address for the network gate, and the record
        # carries every address the machine answers at.
        context["is_this_device"] = forwarding_of(self.request).reported in record["addresses"]
        context["hq_label"] = HQ_LABEL
        context["page_aside"] = "control_plane/_machine_status.html"
        context["page_visit"] = page_visit("machine", record["name"])

        # Whether what each container runs is current and safe, as the
        # containers page says it, and what the machine keeps that nothing runs.
        context.update(on_machine(found))
        # Each line under the part it is about, as HQ's own page draws them.
        context["problems_in"] = grouped(problems_by_part(problem_parts(record), principal))
        # The same panel as the tailnet page, started on this machine. Asked
        # here it is nearly always about this one, so both ends default to it
        # and changing either is one dropdown rather than three.
        # From this device to the machine HQ runs on, which is the question a
        # machine page is usually asked. On HQ's own machine the target is left
        # for the operator to choose.
        own = record["presence"]["tailnet_name"] if record["presence"] else ""
        hq = next((item for item in machines_once() if item.runs_hq), None)
        hq_device = hq.presence.tailnet_name if hq and hq.presence else ""
        context.update(
            whatif_context(
                self.request,
                default=own,
                target_default="" if hq_device == own else hq_device,
            )
        )
        return context

"""Machines and the tailnet: the list, one machine's page, and what a change to either would reach."""

from __future__ import annotations

from urllib.parse import urlencode

from django.http import Http404
from django.shortcuts import redirect
from hq.platform.application.routes import reverse
from django.views.generic import TemplateView

from hq.platform.application.connections import machines_once
from hq.platform.application.machine_context import machine_links, sections_for as machine_sections
from hq.platform.application.tailnet_context import tailnet_context
from hq.platform.application.hq_self import LABEL as HQ_LABEL
from hq.platform.application.machines import declaration_seed, machine
from hq.platform.application.security import web_principal
from hq.platform.application.pages import PageAction, PageMixin
from hq.platform.application.resource_capabilities import resource_capabilities

from .models import ManagedResource
from .provider_adapters.declarations import MACHINE_KIND
from .provider_adapters.portainer import CONTAINER_KIND


class MachineListView(PageMixin, TemplateView):
    """Every machine anything reported, and what is on each.

    Nothing here is declared. A machine exists because a credential reaches it,
    a container runs on it, or a service is served from it, so adding a VPS is
    registering it somewhere rather than entering it here.
    """

    template_name = "control_plane/machine_list.html"
    page_title = "Machines"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["machines"] = machines_once()
        context["hq_label"] = HQ_LABEL
        return context


def whatif_context(request, default: str = "", *, target_default: str | None = None) -> dict:
    """Everything the reachability panel needs, wherever it is included.

    One function because the panel appears on more than one page and the two
    must not drift into disagreeing about what they were asked. ``default``
    is the device a page is already about, so opening the panel there starts
    with the question most likely being asked.
    """

    from hq.platform.application.tailnet import (
        declaration,
        devices,
        may_reach,
        ports,
        proposed_grant,
    )

    known = devices()
    asked = {
        "source": request.GET.get("source", "") or default,
        "target": request.GET.get("target", "")
        or (default if target_default is None else target_default),
        "port": request.GET.get("port", ""),
    }
    context = {
        "device_names": sorted(known),
        "ports": ports(),
        "asked": asked,
        "whatif_action": request.path,
        "policy_declaration": declaration(),
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
            context["proposal"] = proposed_grant(
                asked["source"], asked["target"], int(asked["port"])
            )
    return context


class TailnetView(PageMixin, TemplateView):
    """The tailnet, and whether one machine may reach another.

    A page because the question has nowhere else to live. Reachability is not a
    property of any one declaration: it is the policy's answer about a pair,
    so it belongs beside the devices rather than on any of them.
    """

    template_name = "control_plane/tailnet.html"
    page_title = "Tailnet"

    def get_page_actions(self):
        policy_declaration = self.tailnet.declaration
        return (
            PageAction(
                "Policy test",
                reverse("control_plane:tailnet"),
                primary=True,
                modal="whatif",
            ),
            *(
                (
                    PageAction(
                        "Edit policy",
                        reverse("control_plane:edit", args=[policy_declaration]),
                    ),
                )
                if policy_declaration
                else ()
            ),
            PageAction("All machines", reverse("control_plane:machines")),
        )

    def get(self, request, *args, **kwargs):
        self.tailnet = tailnet_context(principal=web_principal(request.user))
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(whatif_context(self.request))
        context["tailnet"] = self.tailnet
        return context


class MachineDetailView(PageMixin, TemplateView):
    """One machine, and everything that ties to it.

    A container's host, a proxy's forwarding address, what a Portainer says it
    reaches and which credential opens a shell there are four facts about one
    thing, and this is the page where they meet.
    """

    template_name = "control_plane/machine_detail.html"

    def get(self, request, *args, **kwargs):
        self.found = machine(kwargs["name"])
        if self.found is None:
            raise Http404("No machine of that name has been reported.")
        if self.found.name.lower() != kwargs["name"].strip().lower():
            # Asked for by another of its names; the page lives at one.
            return redirect("control_plane:machine", name=self.found.name)
        return super().get(request, *args, **kwargs)

    def get_page_title(self):
        return self.found.name

    def get_page_trail(self):
        return (("Machines", reverse("control_plane:machines")),)

    def get_page_actions(self):
        # The machine's declaration is edited from its own page.
        if self.found.declaration:
            key = self.found.declaration
            return tuple(
                [
                    PageAction("Edit machine", reverse("control_plane:edit", args=[key])),
                    PageAction(
                        "Remove", reverse("control_plane:remove", args=[key]), danger=True
                    ),
                ]
            )
        # Seeded with what HQ knows, and back here after saving. A machine the
        # tailnet device declaration already names gets its details added; one
        # with no declaration at all is declared.
        seeded = urlencode(
            {
                "kind": MACHINE_KIND,
                **declaration_seed(self.found),
                "next": self.found.url,
            },
            doseq=True,
        )
        return (
            PageAction(
                "Add machine details" if self.found.route_approval_key else "Declare machine",
                f"{reverse('control_plane:create')}?{seeded}",
            ),
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        found = self.found
        context["machine"] = found
        device = (
            ManagedResource.objects.filter(key=found.route_approval_key).first()
            if found.route_approval_key
            else None
        )
        context["route_approval"] = (
            resource_capabilities(device, running=()).actions.get("approve-routes")
            if device is not None
            else None
        )
        context["route_approval_off"] = bool(
            context["route_approval"] and not context["route_approval"].enabled
        )
        # What else HQ can say about this machine, from a registry rather than
        # from this view. A band appears because a resolver produced one, so
        # what HQ learns next reaches the page without either being edited.
        context["sections"] = machine_sections(found)
        from hq.platform.application.page_relations import for_machine

        whole, context["relationships"] = for_machine(
            found, context["sections"], principal=web_principal(self.request.user)
        )
        context.update(machine_links(found, whole))
        # Whether you are reading this on the machine it describes. HQ already
        # judged the caller's address for the network gate, and every machine
        # carries the addresses it answers at, so the page could always have
        # known, and said "this machine" while you looked at your own laptop.
        # Arithmetic on one address: no query, no sweep.
        from hq.platform.application.request_channel import displayed_client_ip

        context["is_this_device"] = displayed_client_ip(self.request) in found.addresses
        context["hq_label"] = HQ_LABEL
        context["container_kind"] = CONTAINER_KIND
        from hq.platform.application.containers import on_machine
        from hq.platform.application.machine_context import header_addresses

        # Whether what each container runs is current and safe, as the
        # containers page says it, and what the machine keeps that nothing runs.
        context.update(on_machine(found))
        context["header_addresses"] = header_addresses(found)
        # The same panel as the tailnet page, started on this machine. Asked
        # here it is nearly always about this one, so both ends default to it
        # and changing either is one dropdown rather than three.
        # From this device to the machine HQ runs on, which is the question a
        # machine page is usually asked. On HQ's own machine the target is left
        # for the operator to choose.
        own = found.presence.tailnet_name if found.presence else ""
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

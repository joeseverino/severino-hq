"""Services: the catalogue, one service's page, publishing a new one, and the favourites order."""

from __future__ import annotations

from functools import cached_property

from django.contrib import messages

from django.http import Http404
from django.shortcuts import redirect, render
from hq.platform.application.routes import reverse
from django.views import View
from django.views.generic import TemplateView

from hq.platform.application.adoption import unmanaged_services
from hq.platform.application.inventory import inventory_state
from hq.platform.application.connections import machines_once
from hq.platform.application.entity_links import entity_link
from hq.platform.application.exposure import OPEN
from hq.platform.application.exposure_fixes import gate_links
from hq.platform.application.hq_self import LABEL as HQ_LABEL, hq_service
from hq.platform.application.service_list import listed_service, listed_services
from hq.platform.application.services import alias_target
from hq.platform.application.service_facets import CERTIFICATE_FACET, DNS_FACET, RUNTIME_FACET
from hq.platform.application.security import safe_next, web_principal
from hq.platform.application.service_context import missing_facets, page_parts, sections_for, service_badges, service_summary
from hq.platform.application.pages import PageAction, PageMixin, page_context
from hq.platform.application.resource_capabilities import (
    LIFECYCLE_VERBS,
    VERB_LABELS,
    resource_capabilities,
)
from hq.platform.application.ui import PageNavigation, PageSection

from .models import ManagedResource
from .names import normalized_hostname
from .provider_adapters.portainer import CONTAINER_KIND
from .providers import service_facets


class ServiceListView(PageMixin, TemplateView):
    """The hostname view of the same declarations the resource list shows."""

    template_name = "control_plane/service_list.html"
    page_title = "Services"

    def get_page_actions(self):
        # "Add" on the services board starts a service, not the resource picker.
        return (
            PageAction(
                "Publish a service",
                reverse("control_plane:service_start"),
                primary=True,
            ),
        )

    def get_context_data(self, **kwargs):
        from hq.platform.application.pins import SERVICE, ordered
        from hq.platform.application.service_facets import RUNTIME_FACET

        context = super().get_context_data(**kwargs)
        favorites = ordered(self.request.user, SERVICE)
        # HQ's own name and observed names are rows like any other, marked.
        found = listed_services(favorites)
        # One table, a group each: the few an operator keeps at the top answer
        # "is my stuff healthy", the rest "what else is out there", and
        # reordering only means anything within the first. One table is what
        # keeps both on one set of columns without fixing their widths.
        pinned = [item for item in found if item.pinned]
        rest = [item for item in found if not item.pinned]
        context["service_groups"] = tuple(
            group
            for group in (("Favorites", pinned, True), ("Other services", rest, False))
            if group[1]
        )
        # One answer for both groups, which share the table's columns.
        context["service_projects"] = any(item.project for item in found)
        # Where a service runs is one column, not two. The runtime facet named
        # the container declaration and the origin named the machine it runs
        # on, side by side, in two different vocabularies for one fact.
        context["runtime_facet"] = RUNTIME_FACET
        # The column headers come from the providers, so a provider that
        # declares a new facet gets a column without this template being
        # touched, and a facet no provider supplies yet gets none.
        context["facets"] = [
            facet for facet in service_facets() if facet[0] != RUNTIME_FACET
        ]
        # Everything the providers hold that no declaration accounts for. Shown
        # beside the managed services rather than on a page of its own: a
        # hostname HQ does not manage is still a hostname that is serving, and
        # hiding it is how a console ends up describing only the tidy half of
        # the estate.
        context["unmanaged"] = unmanaged_services()
        context["inventory"] = inventory_state()
        context["certificate_facet"] = CERTIFICATE_FACET
        context["dns_facet"] = DNS_FACET
        return context


class ServiceDetailView(PageMixin, TemplateView):
    """One hostname, whether or not anything has been declared for it yet.

    A name with nothing behind it still has a page: it is where publishing a
    service starts.
    """

    template_name = "control_plane/service_detail.html"

    def get(self, request, *args, **kwargs):
        """An alias goes to the service it is an alias of.

        Rendered here, the page could only report that nothing supplied a name
        whose record is declared, healthy and listed on the domain page: HQ
        contradicting itself about its own data.
        """

        if not entity_link("service", kwargs["hostname"]).url:
            raise Http404("Not a host name.")
        canonical = alias_target(kwargs["hostname"])
        if canonical:
            return redirect("control_plane:service", hostname=canonical)
        return super().get(request, *args, **kwargs)

    @cached_property
    def service(self):
        return listed_service(self.kwargs["hostname"])

    @cached_property
    def own(self):
        """HQ's own service, when this page is about it. Derived and read-only."""

        own = hq_service(catalog=machines_once())
        if own is not None and self.service.hostname in own.hostnames:
            return own
        return None

    @cached_property
    def relationships(self):
        from hq.platform.application.page_relations import for_service

        return for_service(self.service, self.sections, principal=web_principal(self.request.user))

    @cached_property
    def missing_facets(self) -> list:
        return missing_facets(self.service)

    @cached_property
    def sections(self):
        # Everything else HQ holds about this name, gathered by the name. Only
        # here: the board builds every service and needs none of it.
        return sections_for(self.service)

    def get_page_title(self):
        return self.service.hostname

    def get_page_trail(self):
        return (("Services", reverse("control_plane:services")),)

    def get_page_navigation(self):
        return PageNavigation(
            (
                PageSection("overview", "Overview"),
                PageSection("path", "Path"),
                *((PageSection("parts", "Not set up"),) if self.missing_facets else ()),
                *(PageSection(section.id, section.label) for section in self.sections),
                *(
                    (PageSection("relationships", "Relationships"),)
                    if self.relationships.groups
                    else ()
                ),
                *(
                    (PageSection("resources", "Other names"),)
                    if self.service.alias_claims
                    else ()
                ),
            )
        )

    def get_page_actions(self):
        """The container's verbs, then the way to the domain.

        In the page head rather than the card that describes the container.
        Which verbs appear is the watching declaration's capabilities, given
        the container's current state.
        """

        if self.own is not None:
            # HQ does not act on itself from its own page.
            return ()
        container = self.service.container
        actions = []
        watcher = (
            ManagedResource.objects.filter(key=container.watcher).first()
            if container and container.watcher
            else None
        )
        if watcher is not None:
            capabilities = resource_capabilities(watcher, running=container.verbs)
            actions.extend(
                PageAction(
                    VERB_LABELS[verb],
                    reverse(f"control_plane:{verb}", kwargs={"key": watcher.key}),
                    method="post",
                    disabled=not allowed.enabled,
                    title=allowed.reason or f"{VERB_LABELS[verb]} {container.name}",
                )
                for verb, allowed in capabilities.page_actions
                if verb in LIFECYCLE_VERBS
            )
        elif container:
            actions.append(
                PageAction(
                    "Watch container",
                    reverse(
                        "control_plane:adopt_record",
                        kwargs={"kind": CONTAINER_KIND, "token": container.token},
                    ),
                    method="post",
                    title=f"Let HQ start, stop and restart {container.name}",
                )
            )
        if self.service.zone_key:
            actions.append(
                PageAction("Domain", reverse("zones:detail", args=[self.service.zone_key]))
            )
        return tuple(actions)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["service"] = self.service
        context["summary"] = service_summary(self.service)
        context["page_badges"] = service_badges(self.service, own=self.own is not None)
        context.update(page_parts(self.service, self.request))
        context["missing_facets"] = self.missing_facets
        # The trace of this name in the topology, beside the path it draws.
        context["topology_url"] = self.relationships.focus_url
        context["container_kind"] = CONTAINER_KIND
        context["sections"] = self.sections
        context["relationships"] = self.relationships
        context["read_only"] = self.own is not None
        # Who reaches it, and, when that is anyone on the internet unasked,
        # the gate HQ can put in front.
        exposure = self.service.exposure
        context["exposure"] = exposure
        context["gate_fixes"] = gate_links(self.service) if exposure.level == OPEN else ()
        context["hq_label"] = HQ_LABEL
        context["runtime_facet"] = RUNTIME_FACET
        context["hq_machine"] = (
            entity_link("machine", self.own.machine) if self.own and self.own.machine else None
        )
        return context


class ServiceStartView(View):
    """Ask for a hostname, then stand on its page.

    The whole of "publish a service" is knowing the name. Everything after it
    is already offered, seeded, by the page that name leads to.
    """

    def get(self, request):
        return render(
            request,
            "control_plane/service_start.html",
            page_context("Publish a service", "Enter a hostname to see what it needs."),
        )

    def post(self, request):
        hostname = normalized_hostname(request.POST.get("hostname", ""))
        if not hostname or " " in hostname or "." not in hostname:
            messages.error(request, "Enter a hostname, like app.example.com.")
            return redirect("control_plane:service_start")
        return redirect("control_plane:service", hostname=hostname)


class ServicePinView(View):
    """Keep a service at the top of the list, for this operator only.

    A preference, so it never touches a spec: starring a hostname does not
    bump a generation, queue a reconcile, or change anything about the world.
    """

    def post(self, request, hostname: str):
        from hq.platform.application.pins import SERVICE, toggle

        name = normalized_hostname(hostname)
        if not name:
            raise Http404("No such service.")
        toggle(request.user, SERVICE, name)
        return redirect(safe_next(request) or reverse("control_plane:services"))


class ServiceMoveView(View):
    """Move one favorite past its neighbour.

    Up and down rather than dragging: it is one POST, it works without script,
    and it says out loud which two things swapped, which a drag does not.
    """

    def post(self, request, hostname: str):
        from hq.platform.application.pins import SERVICE, move

        name = normalized_hostname(hostname)
        if not name:
            raise Http404("No such service.")
        delta = -1 if request.POST.get("direction") == "up" else 1
        move(request.user, SERVICE, name, delta)
        return redirect(safe_next(request) or reverse("control_plane:services"))

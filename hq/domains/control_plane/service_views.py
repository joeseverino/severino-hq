"""Services: the catalog, one service's page, publishing a new one, and the favorites order."""

from functools import cached_property
from typing import override

from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import TemplateView

from hq.platform.application.adoption import ADOPT, ADOPT_SERVICE, unmanaged_services
from hq.platform.application.connections import machines_once
from hq.platform.application.entity_links import entity_link, page_url
from hq.platform.application.exposure import OPEN
from hq.platform.application.exposure_fixes import gate_links
from hq.platform.application.hq_self import LABEL as HQ_LABEL, hq_service
from hq.platform.application.inventory import inventory_state
from hq.platform.application.labels import sentence
from hq.platform.application.pages import PageAction, PageMixin, RecordPageMixin, RowMark, RowOrder, page_context
from hq.platform.application.references import Kind
from hq.platform.application.resource_capabilities import (
    LIFECYCLE_VERBS,
    resource_capabilities,
)
from hq.platform.application.routes import reverse
from hq.platform.application.security import safe_next, web_principal
from hq.platform.application.service_context import (
    missing_facets,
    page_parts,
    sections_for,
    service_badges,
)
from hq.platform.application.service_facets import RUNTIME_FACET
from hq.platform.application.service_list import listed_service, listed_services
from hq.platform.application.services import alias_summary, alias_target, fault_rows
from hq.platform.application.tone import Absent
from hq.platform.application.ui import PageNavigation, PageSection
from hq.platform.application.visit_refresh import page_visit
from hq.platform.application.web_writes import execute_posted
from hq.platform.core.web_refusals import refused
lazy from hq.platform.application.page_relations import for_service
lazy from hq.platform.application.pins import FAVORITES, MOVE, PIN, SERVICE, ordered

from .forms import ServiceStartForm
from .models import ManagedResource
from .provider_adapters.portainer import CONTAINER_KIND
from .providers import service_facets
from .service_tables import aliases_table, parts_table, services_table, unmanaged_table
from .views import verb_action


def _arranged(items, reorderable: bool) -> list[tuple]:
    """Each service with the star that makes it a favorite and, among the
    favorites (``reorderable``), the pair that moves it, where the order is
    one the operator chose; the first has nowhere up to go, the last nowhere
    down."""

    last = len(items) - 1
    return [
        (
            item,
            RowMark.command(PIN, item.hostname, mode=SERVICE, on=item.pinned, among=FAVORITES),
            RowOrder.command(MOVE, item.hostname, mode=SERVICE, up=at > 0, down=at < last) if reorderable else None,
        )
        for at, item in enumerate(items)
    ]


class ServiceListView(PageMixin, TemplateView):
    """The hostname view of the same declarations the resource list shows."""

    template_name = "control_plane/service_list.html"
    page_title = "Services"
    page_reads = ("cloudflare.dns_record", "npm.proxy_host", "adguard.rewrite", "caddy.route")

    @override
    def get_page_actions(self):
        # "Add" on the services board starts a service, not the resource picker.
        return (
            PageAction(
                "Publish a service",
                reverse("control_plane:service_start"),
                primary=True,
            ),
        )

    @override
    def get_context_data(self, **kwargs):
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
        groups = tuple(
            (label, _arranged(items, reorderable))
            for label, items, reorderable in ((sentence(FAVORITES), pinned, True), ("Other services", rest, False))
            if items
        )
        # Where a service runs is one column, not two. The runtime facet named
        # the container declaration and the origin named the machine it runs
        # on, side by side, in two different vocabularies for one fact. The
        # columns come from the providers, so a provider that declares a new
        # facet gets a column without this page being touched, and a facet no
        # provider supplies yet gets none.
        context["services"] = services_table(
            groups,
            [facet for facet in service_facets() if facet[0] != RUNTIME_FACET],
            projects=any(item.project for item in found),
            empty="No services yet.",
            start_url=reverse("control_plane:service_start"),
        )
        # Everything the providers hold that no declaration accounts for. Shown
        # beside the managed services rather than on a page of its own: a
        # hostname HQ does not manage is still a hostname that is serving, and
        # hiding it is how a console ends up describing only the tidy half of
        # the estate.
        context["unmanaged"] = unmanaged = unmanaged_services()
        # A name read through a connection that manages can be taken under
        # HQ's management from its row; one only observed cannot.
        context["unmanaged_rows"] = [
            (
                service,
                ()
                if service.observed_only
                else tuple(filter(None, (PageAction.command(ADOPT_SERVICE, service.hostname),))),
            )
            for service in unmanaged
        ]
        context["unmanaged_table"] = unmanaged_table(context["unmanaged_rows"])
        context["inventory"] = inventory_state()
        return context


class ServiceDetailView(RecordPageMixin, TemplateView):
    """One hostname, whether or not anything has been declared for it yet.

    A name with nothing behind it still has a page, its record a prospect:
    it is where publishing a service starts.
    """

    template_name = "control_plane/service_detail.html"
    resource = "services"
    context_object_name = "record"

    @override
    def get(self, request, *args, **kwargs):
        """An alias goes to the service it is an alias of.

        Rendered here, the page could only report that nothing supplied a name
        whose record is declared, healthy and listed on the domain page: HQ
        contradicting itself about its own data.
        """

        canonical = alias_target(kwargs["hostname"])
        if canonical:
            return redirect(page_url(Kind.SERVICE, canonical))
        return super().get(request, *args, **kwargs)

    @cached_property
    def service(self):
        return listed_service(self.record["hostname"])

    @cached_property
    def own(self):
        """HQ's own service, when this page is about it. Derived and read-only."""

        own = hq_service(catalog=machines_once())
        if own is not None and self.service.hostname in own.hostnames:
            return own
        return None

    @cached_property
    def relationships(self):
        return for_service(self.service, self.sections, principal=web_principal(self.request.user))

    @cached_property
    def missing_facets(self) -> list:
        return missing_facets(self.service)

    @cached_property
    def sections(self):
        # Everything else HQ holds about this name, gathered by the name. Only
        # here: the board builds every service and needs none of it.
        return sections_for(self.service)

    page_trail = (("Services", reverse_lazy("control_plane:services")),)

    @override
    def get_page_navigation(self):
        return PageNavigation(
            (
                PageSection("overview", "Overview"),
                PageSection("path", "Path"),
                *((PageSection("parts", sentence(Absent.NOT_SET_UP)),) if self.missing_facets else ()),
                *(PageSection(section.id, section.label) for section in self.sections),
                *((PageSection("relationships", "Relationships"),) if self.relationships.groups else ()),
                *((PageSection("resources", "Other names"),) if self.record["alias_records"] else ()),
            )
        )

    @override
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
            ManagedResource.objects.filter(key=container.watcher).first() if container and container.watcher else None
        )
        if watcher is not None:
            capabilities = resource_capabilities(watcher, running=container.verbs)
            actions.extend(
                verb_action(verb, watcher.key, allowed, origin=self.request.get_full_path())
                for verb, allowed in capabilities.page_actions
                if verb in LIFECYCLE_VERBS
            )
        elif container:
            actions.append(
                PageAction.command(ADOPT, {"kind": CONTAINER_KIND, "token": container.token}, variant=CONTAINER_KIND)
            )
        if self.service.zone_key:
            actions.append(PageAction("Domain", page_url(Kind.ZONE, self.service.zone_key)))
        return tuple(actions)

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["service"] = self.service
        context["page_visit"] = page_visit("service", self.record["hostname"])
        context["page_badges"] = service_badges(self.record["exposure"]["reach"], own=self.own is not None)
        context.update(page_parts(self.service, self.request))
        context["missing_facets"] = self.missing_facets
        # The trace of this name in the topology, beside the path it draws.
        context["topology_url"] = self.relationships.focus_url
        context["container_kind"] = CONTAINER_KIND
        context["sections"] = self.sections
        context["relationships"] = self.relationships
        context["read_only"] = read_only = self.own is not None
        here = self.request.get_full_path()
        context["route_tables"] = [
            (route, parts_table(parts, read_only=read_only, here=here)) for route, parts in context["route_parts"]
        ]
        if "declared_parts" in context:
            context["declared_table"] = parts_table(context["declared_parts"], read_only=read_only, here=here)
        context["aliases"] = aliases_table(self.record["alias_records"])
        # When anyone on the internet reaches it unasked, the gate HQ can put in front.
        context["gate_fixes"] = gate_links(self.service) if self.record["exposure"]["level"] == OPEN else ()
        context["fault_rows"] = fault_rows(self.record["hostname"], faults) if (faults := self.record["faults"]) else ()
        context["alias_summary"] = alias_summary(self.record["aliases"])
        context["hq_label"] = HQ_LABEL
        context["runtime_facet"] = RUNTIME_FACET
        context["hq_machine"] = entity_link("machine", self.own.machine) if self.own and self.own.machine else None
        return context


class ServiceStartView(View):
    """Ask for a hostname, then stand on its page.

    The whole of "publish a service" is knowing the name. Everything after it
    is already offered, seeded, by the page that name leads to.
    """

    def get(self, request):
        """The question, or with its answer the page that name leads to: a
        lookup, so its form is a GET and sending it writes nothing."""

        if "hostname" not in request.GET:
            return self._page(request, ServiceStartForm())
        form = ServiceStartForm(request.GET)
        if not form.is_valid():
            return self._page(request, form, status=400)
        return redirect(page_url(Kind.SERVICE, form.cleaned_data["hostname"]))

    def _page(self, request, form, status=200):
        return render(
            request,
            "control_plane/service_start.html",
            {**page_context("Publish a service", "Enter a hostname to see what it needs."), "form": form},
            status=status,
        )


class ServicePinView(View):
    """Keep a service at the top of the list, for this operator only.

    A preference, so it never touches a spec: starring a hostname does not
    bump a generation, queue a reconcile, or change anything about the world.
    """

    capability = PIN

    # A pin of a service, the mode of the command its route runs: a domain's
    # star is the command's own control.
    @property
    def runs(self) -> tuple[tuple[str, str], ...]:
        return ((self.capability, SERVICE),)

    def payload(self, name: str) -> dict:
        return {"kind": SERVICE, "key": name}

    def post(self, request, hostname: str):
        refused(request, execute_posted(request, self.capability, self.payload(hostname)))
        return redirect(safe_next(request, fallback=reverse("control_plane:services")))


class ServiceMoveView(ServicePinView):
    """Move one favorite past its neighbor.

    Up and down rather than dragging: it is one POST, it works without script,
    and it says out loud which two things swapped, which a drag does not.
    """

    capability = MOVE

    def payload(self, name: str) -> dict:
        return {"kind": SERVICE, "key": name, "up": self.request.POST.get("direction") == "up"}

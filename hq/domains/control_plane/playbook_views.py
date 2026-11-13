"""The Playbooks page: the playbooks HQ runs from, the commit they were
adopted at, and, while a newer scanned commit waits, a person's decision on it.

Every value is the catalog's or a scan's (``automation_sources.standing``);
the host names no procedure, machine or repository, and the source's
repository is the deployment's own configuration, so the page names a
commit by its branch and id alone. What a scanned commit changes is its
scan's own page (``jobs:run``), which View diff opens.
"""

from dataclasses import dataclass
from functools import cached_property
from typing import Any, ClassVar, override

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from django.shortcuts import redirect
from django.views import View
from django.views.generic import TemplateView

from hq.domains.control_plane.models import SourceProposal
from hq.platform.application.action_links import capability_action_link, command_url
from hq.platform.application.automation_adoption import ADOPT, REJECT
from hq.platform.application.automation_runs import access_words, anywhere, capability_name
from hq.platform.application.automation_sources import REFRESH, SourceStanding, decisions, scan_standing, standing
from hq.platform.application.pages import PageMixin
from hq.platform.application.routes import reverse
from hq.platform.application.security import web_principal
from hq.platform.application.web_writes import execute_posted, refused
lazy from hq.domains.jobs.models import JobState
lazy from hq.platform.application.asks import FAILED, WATCH_LIMIT, Ask, Standing, answer, watch_token, watched
lazy from hq.platform.application.labels import effect_label
lazy from hq.platform.application.refusal_status import WANTS_A_GRANT, refusal_of
lazy from hq.platform.application.work_runs import TONES, run_url
lazy from hq.platform.core.templatetags.action_tags import MOST_BESIDE


def scan_status_url(operation_id: str) -> str:
    """Where a page follows one scan, signed so it reports on nothing else."""

    return f"{reverse('control_plane:playbooks_status')}?watch={watch_token({'scan': operation_id})}"


@dataclass(frozen=True, slots=True)
class CatalogRow:
    """One procedure of the catalog adopted now, as its table row shows it."""

    procedure: Any
    url: str
    effect: str
    # Where it runs when it names no machine.
    anywhere: str
    access: str


class PlaybooksView(PageMixin, TemplateView):
    """The adopted catalog, and what to do about a newer scanned commit."""

    template_name = "control_plane/playbooks.html"
    page_title = "Playbooks"

    @cached_property
    def sources(self) -> SourceStanding:
        return standing()

    @cached_property
    def principal(self):
        return web_principal(self.request.user)

    @override
    def get_page_actions(self):
        refresh = capability_action_link(REFRESH, principal=self.principal)
        scanned, scan = self.sources.scanned, self.sources.scan
        # A page loaded while a scan is under way follows it; one that ended
        # is said on the title's line (``_playbooks_read.html``).
        live = scan is not None and scanned.live
        asked = (
            Ask(
                refresh.label,
                reverse("control_plane:playbooks_refresh"),
                standing=scanned if live else Standing(),
                status_url=scan_status_url(str(scan.pk)) if live else "",
                title="Read your repository for a newer commit.",
            )
            if refresh is not None
            else None
        )
        return (*((asked,) if asked else ()), *decisions(self.sources.pending, self.principal, diff=True))

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        sources = self.sources
        context.update(
            page_aside="control_plane/_playbooks_read.html",
            # Refresh, then View diff, Adopt all and Reject all side by side while a commit waits.
            page_beside=MOST_BESIDE if sources.pending else "",
            failed_tone=(
                TONES[JobState(sources.reached.seen)]
                if sources.reached is not None and sources.reach.state == FAILED
                else ""
            ),
            sources=sources,
            # When HQ last reached the repository: a scan result is kept only by a read that succeeded.
            read_at=SourceProposal.objects.order_by("-pk").values_list("created_at", flat=True).first(),
            adopted=[
                CatalogRow(
                    procedure,
                    command_url(capability_name(procedure.id)),
                    effect_label(procedure.effect),
                    anywhere(procedure.target_kinds),
                    access_words(procedure.privilege),
                )
                for procedure in sources.procedures
            ],
        )
        return context


class RefreshView(View):
    """Ask HQ's runner to scan the repository now: the scan is
    queued and the request answers at once, and ``ScanStatusView`` says how
    it stands."""

    http_method_names: ClassVar[list[str]] = ["post"]

    def post(self, request):
        back = reverse("control_plane:playbooks")
        result = execute_posted(request, REFRESH, {})
        if not result.get("ok"):
            return answer(request, Standing(FAILED, refusal_of(result).message), fallback=back)
        operation = str(result["operation"]["id"])
        return answer(
            request,
            scan_standing(operation) or Standing(),
            fallback=back,
            status_url=scan_status_url(operation),
        )


class ScanStatusView(View):
    """How a scan stands, for a page following it: only one HQ handed that
    page to watch, signed."""

    http_method_names: ClassVar[list[str]] = ["get"]

    def get(self, request):
        token = str(request.GET.get("watch", ""))
        found = watched(token) if 0 < len(token) <= WATCH_LIMIT else None
        scan = scan_standing(str(found["scan"])) if found and isinstance(found.get("scan"), str) else None
        if scan is None:
            raise Http404("Nothing to watch.")
        return JsonResponse(scan.as_json())


class ProposalDecisionView(View):
    """Adopt or reject one scan result through its command, so a press is
    authorized and recorded as the same call through the API. A refusal goes
    back to the scan's page, which says what is wrong with the commit."""

    http_method_names: ClassVar[list[str]] = ["post"]
    capability: ClassVar[str]
    # The answer's field that says it changed anything, and what the next
    # page says when it did and when it did not.
    result_field: ClassVar[str]
    done: ClassVar[tuple[str, str]]

    def post(self, request, pk: int):
        result = execute_posted(request, self.capability, {"proposal": pk})
        if not result.get("ok") and refusal_of(result).code in WANTS_A_GRANT:
            raise PermissionDenied(refusal_of(result).message)
        if refused(request, result) is not None:
            scan = SourceProposal.objects.filter(pk=pk).values_list("operation_id", flat=True).first()
            return redirect(run_url(str(scan)) if scan else reverse("control_plane:playbooks"))
        changed, unchanged = self.done
        if result.get(self.result_field):
            messages.success(request, changed)
        else:
            messages.info(request, unchanged)
        return redirect("control_plane:playbooks")


class ProposalAdoptView(ProposalDecisionView):
    """Adopt one scan result: its playbooks become the ones every playbook's command is built from."""

    capability = ADOPT
    result_field = "adopted"
    done = ("Adopted.", "That commit is already adopted.")


class ProposalRejectView(ProposalDecisionView):
    """Reject one scan result, so nothing waits to be adopted."""

    capability = REJECT
    result_field = "rejected"
    done = ("Rejected.", "That commit is already rejected.")

"""The procedures page: the catalog the automation worker runs from, the
newest scan of its repository, and a proposal a person reviews and adopts.

Every value is the catalog's or a scan's (``automation_sources.standing``);
the host names no procedure, machine or repository, and the source's
repository is the deployment's own configuration, so the page names a
commit by its branch and id alone.
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

from hq.platform.application.action_links import capability_action_link, command_url
from hq.platform.application.automation_adoption import ADOPT
from hq.platform.application.automation_runs import capability_name
from hq.platform.application.automation_sources import REFRESH, SourceStanding, scan_standing, standing
from hq.platform.application.pages import PageMixin
from hq.platform.application.record_errors import RecordNotFound
from hq.platform.application.routes import reverse
from hq.platform.application.security import web_principal
from hq.platform.application.web_writes import execute_posted, refused
lazy from hq.domains.jobs.models import JobState
lazy from hq.platform.application.approvals import is_held
lazy from hq.platform.application.asks import FAILED, WATCH_LIMIT, Ask, Standing, answer, watch_token, watched
lazy from hq.platform.application.labels import effect_label
lazy from hq.platform.application.refusal_status import WANTS_A_GRANT, refusal_of
lazy from hq.platform.application.work_runs import TONES


def scan_status_url(operation_id: str) -> str:
    """Where a page follows one scan, signed so it reports on nothing else."""

    return f"{reverse('control_plane:procedures_status')}?watch={watch_token({'scan': operation_id})}"


@dataclass(frozen=True, slots=True)
class CatalogRow:
    """One procedure of the catalog adopted now, as its table row shows it."""

    procedure: Any
    url: str
    effect: str


class ProceduresView(PageMixin, TemplateView):
    """The adopted catalog, the newest scan and the newest proposal; with a
    proposal's id, that proposal instead."""

    template_name = "control_plane/procedures.html"
    page_lede = "What the automation worker may run: the procedures of the commit of its repository a person adopted."

    @cached_property
    def sources(self) -> SourceStanding:
        try:
            return standing(self.kwargs.get("pk"))
        except RecordNotFound as missing:
            raise Http404(str(missing)) from missing

    @cached_property
    def principal(self):
        return web_principal(self.request.user)

    @override
    def get_page_title(self) -> str:
        review = self.sources.proposal
        if "pk" in self.kwargs and review is not None:
            return f"Proposal {review.revision}"
        return "Procedures"

    @override
    def get_page_trail(self):
        return (("Procedures", reverse("control_plane:procedures")),) if "pk" in self.kwargs else ()

    @override
    def get_page_actions(self):
        refresh = capability_action_link(REFRESH, principal=self.principal)
        if refresh is None:
            return ()
        scanned, scan = self.sources.scanned, self.sources.scan
        # A page loaded while a scan is under way follows it; one that ended
        # is said in the page's own section on the last scan.
        live = scan is not None and scanned.live
        return (
            Ask(
                refresh.label,
                reverse("control_plane:procedures_refresh"),
                standing=scanned if live else Standing(),
                status_url=scan_status_url(str(scan.pk)) if live else "",
                title="Ask the automation worker which procedures its repository holds now.",
            ),
        )

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        sources = self.sources
        review = sources.proposal
        adoptable = review is not None and review.preview is not None
        context.update(
            sources=sources,
            review=review,
            scan_tone=TONES[JobState(sources.scan.seen)] if sources.scan is not None else "",
            adopt=capability_action_link(ADOPT, principal=self.principal) if adoptable else None,
            adopted=[
                CatalogRow(procedure, command_url(capability_name(procedure.id)), effect_label(procedure.effect))
                for procedure in sources.procedures
            ],
            # On a proposal's own page, only what waits for it.
            held=[
                held
                for held in sources.held
                if "pk" not in self.kwargs or (review is not None and held.proposal == review.proposal.pk)
            ],
        )
        return context


class RefreshView(View):
    """Ask the automation worker to scan its repository now: the scan is
    queued and the request answers at once, and ``ScanStatusView`` says how
    it stands."""

    http_method_names: ClassVar[list[str]] = ["post"]

    def post(self, request):
        back = reverse("control_plane:procedures")
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


class ProposalAdoptView(View):
    """Adopt one proposal: its procedures become the catalog every
    procedure's command is built from, through the command itself, so a
    press is authorized, held and recorded as the same call through the API."""

    http_method_names: ClassVar[list[str]] = ["post"]

    def post(self, request, pk: int):
        result = execute_posted(request, ADOPT, {"proposal": pk})
        if not result.get("ok") and refusal_of(result).code in WANTS_A_GRANT:
            raise PermissionDenied(refusal_of(result).message)
        if refused(request, result) is not None:
            return redirect("control_plane:procedures_proposal", pk=pk)
        if is_held(result):
            messages.info(request, "Adopting it waits for a person to approve it.")
        elif result.get("adopted"):
            messages.success(request, "Adopted. Its procedures run from this commit now.")
        else:
            messages.info(request, "That commit is the catalog adopted now, so nothing changed.")
        return redirect("control_plane:procedures")

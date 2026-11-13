"""The Playbooks page: the playbooks HQ runs from, when their repository
was last read, and the one commit there is to decide on.

Every value is the catalog's or a scan's (``automation_sources.standing``);
the host names no procedure, machine or repository, and the source's
repository is the deployment's own configuration, so the page names a
commit by its branch and id alone. A held adoption is an approval, drawn by
the card its audit entry draws.
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
from hq.platform.application.automation_adoption import ADOPT, anywhere
from hq.platform.application.automation_runs import capability_name
from hq.platform.application.automation_sources import REFRESH, SourceStanding, scan_standing, standing
from hq.platform.application.pages import PageMixin
from hq.platform.application.record_errors import RecordNotFound
from hq.platform.application.routes import reverse
from hq.platform.application.security import web_principal
from hq.platform.application.web_writes import execute_posted, refused
lazy from hq.domains.jobs.models import JobState
lazy from hq.platform.application.approval_review import review
lazy from hq.platform.application.approvals import is_held
lazy from hq.platform.application.asks import FAILED, WATCH_LIMIT, Ask, Standing, answer, watch_token, watched
lazy from hq.platform.application.labels import effect_label
lazy from hq.platform.application.moments import ago
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
    # Where it runs when it names no machine.
    anywhere: str


class ProceduresView(PageMixin, TemplateView):
    """The adopted catalog and the newest commit to decide on; with a
    proposal's id, that commit instead."""

    template_name = "control_plane/procedures.html"

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
            return f"Commit {review.revision}"
        return "Playbooks"

    @override
    def get_page_lede(self) -> str:
        # Only a refused commit's own page says more than its title and sections.
        found = self.sources.proposal if "pk" in self.kwargs else None
        if found is not None and found.refused:
            return "HQ can't adopt this commit. Fix what's listed below in your repository."
        return ""

    @override
    def get_page_trail(self):
        return (("Playbooks", reverse("control_plane:procedures")),) if "pk" in self.kwargs else ()

    @override
    def get_page_actions(self):
        refresh = capability_action_link(REFRESH, principal=self.principal)
        if refresh is None:
            return ()
        scanned, scan = self.sources.scanned, self.sources.scan
        # A page loaded while a scan is under way follows it; one that ended
        # is said on the title's line (``_procedures_read.html``).
        live = scan is not None and scanned.live
        return (
            Ask(
                refresh.label,
                reverse("control_plane:procedures_refresh"),
                standing=scanned if live else Standing(),
                status_url=scan_status_url(str(scan.pk)) if live else "",
                title="Read your repository for a newer commit.",
            ),
        )

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        sources = self.sources
        # On a commit's own page, only what waits for it.
        held = [
            each.approval
            for each in sources.held
            if "pk" not in self.kwargs
            or (sources.proposal is not None and each.proposal == sources.proposal.proposal.pk)
        ]
        own_page = "pk" in self.kwargs
        found = sources.proposal
        held_commits = {each.proposal for each in sources.held}
        # The commit this page asks about, unless it is the one adopted now,
        # which the adopted section says.
        newest = found if found is not None and not found.adopted else None
        catalog = sources.catalog
        here = reverse("control_plane:procedures")
        adopted_said = _said(catalog, sources.revision, "adopted") if catalog else ""
        if catalog and found is not None and found.adopted and not own_page:
            adopted_said += " It's also the newest commit HQ has read."
        context.update(
            page_aside="control_plane/_procedures_read.html",
            failed_tone=(
                TONES[JobState(sources.reached.seen)]
                if sources.reached is not None and sources.reach.state == FAILED
                else ""
            ),
            sources=sources,
            # When HQ last read the repository: a scan result is kept only by a read that succeeded.
            read_at=SourceProposal.objects.order_by("-pk").values_list("created_at", flat=True).first(),
            review=newest,
            # Its adoption already waits for a person, on the card below.
            review_held=newest is not None and newest.proposal.pk in held_commits,
            held=[_on_this_page(review(approval), here) for approval in held],
            adopt=(
                capability_action_link(ADOPT, principal=self.principal)
                if newest and newest.preview and newest.proposal.pk not in held_commits
                else None
            ),
            review_said=(
                _said(newest.proposal, newest.revision, "read", newest=not own_page) if newest is not None else ""
            ),
            adopted_said=adopted_said,
            own_page=own_page,
            # A commit's own page draws the adopted catalog only when it is that commit.
            shows_adopted="pk" not in self.kwargs or (sources.proposal is not None and sources.proposal.adopted),
            adopted=[
                CatalogRow(
                    procedure,
                    command_url(capability_name(procedure.id)),
                    effect_label(procedure.effect),
                    anywhere(procedure),
                )
                for procedure in sources.procedures
            ],
        )
        return context


def _said(commit, revision: str, done: str, *, newest: bool = False) -> str:
    """A commit in the one line a section's lede gives it: its id, branch and
    committer, and when it was read or adopted; the newest read says so."""

    by = f", committed by {commit.committer}" if commit.committer else ""
    said = f"{revision} on {commit.branch}{by}, {done} {ago(commit.created_at)}."
    return f"The newest commit HQ has read: {said}" if newest else f"Commit {said}"


def _on_this_page(entry: dict[str, Any], here: str) -> dict[str, Any]:
    """An approval card drawn on the page its record lives on links nowhere
    else for it, since its link would be this page."""

    return {**entry, "resource_url": ""} if entry["resource_url"] == here else entry


class RefreshView(View):
    """Ask HQ's runner to scan the repository now: the scan is
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
    """Adopt one scan result: its playbooks become the ones every
    playbook's command is built from, through the command itself, so a
    press is authorized, held and recorded as the same call through the API."""

    http_method_names: ClassVar[list[str]] = ["post"]

    def post(self, request, pk: int):
        result = execute_posted(request, ADOPT, {"proposal": pk})
        if not result.get("ok") and refusal_of(result).code in WANTS_A_GRANT:
            raise PermissionDenied(refusal_of(result).message)
        if refused(request, result) is not None:
            return redirect("control_plane:procedures_proposal", pk=pk)
        if is_held(result):
            messages.info(request, "Adopting this commit waits for approval.")
        elif result.get("adopted"):
            messages.success(request, "Adopted. HQ now runs this commit's playbooks.")
        else:
            messages.info(request, "That commit is already adopted.")
        return redirect("control_plane:procedures")

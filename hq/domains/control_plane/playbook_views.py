"""The Playbooks page: the playbooks HQ runs from, the commit they were
last adopted at, and, while a newer scanned commit waits, how it changes
each playbook and a person's decision on each and on all of them.

Every value is the catalog's or a scan's (``automation_sources.standing``);
the host names no procedure, machine or repository, and the source's
repository is the deployment's own configuration, so the page names a
commit by its branch and id alone. What a scanned commit changes is the
audit entry that records the scan's read (``automation_sources.entry_url``),
which View diff opens.
"""

from functools import cached_property
from typing import ClassVar, override

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.utils import timezone
from django.views import View
from django.views.generic import TemplateView

from hq.domains.control_plane.models import SourceProposal
from hq.platform.application.action_links import capability_action_link
from hq.platform.application.automation_adoption import ADOPT, REJECT
from hq.platform.application.automation_findings import worker_problems
from hq.platform.application.automation_sources import (
    REFRESH,
    SourceStanding,
    decisions,
    entry_url,
    playbook_rows,
    scan_standing,
    standing,
)
from hq.platform.application.pages import PageMixin
from hq.platform.application.routes import reverse
from hq.platform.application.security import safe_next, web_principal
from hq.platform.application.web_writes import execute_posted, refused
lazy from hq.domains.jobs.models import JobState
lazy from hq.platform.application.asks import FAILED, Ask, AskMixin, Standing, Took, WatchView, watch_token
lazy from hq.platform.application.cadence import automation_standing
lazy from hq.platform.application.refusal_status import WANTS_A_GRANT, refusal_of
lazy from hq.platform.application.work_runs import TONES, UNENDED
lazy from hq.platform.core.templatetags.action_tags import MOST_BESIDE


def scan_status_url(operation_id: str) -> str:
    """Where a page follows one scan, signed so it reports on nothing else."""

    return f"{reverse('control_plane:playbooks_status')}?watch={watch_token({'scan': operation_id})}"


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

    @cached_property
    def untaken(self) -> str:
        """Why a Refresh would be refused, while no playbook worker reports; ""."""

        return automation_standing().cannot("refresh", now=timezone.now())

    @override
    def get_page_actions(self):
        refresh = capability_action_link(REFRESH, principal=self.principal)
        scanned, scan = self.sources.scanned, self.sources.scan
        # A page loaded while a scan is under way follows it, and one that
        # has not started says so; one that ended is said on the title's line
        # (``_playbooks_read.html``).
        unended = scan is not None and scan.state in UNENDED
        live = unended and scanned.live
        asked = (
            Ask(
                refresh.label,
                reverse("control_plane:playbooks_refresh"),
                standing=scanned if unended else Standing(),
                status_url=scan_status_url(str(scan.pk)) if live else "",
                title=self.untaken or "Read your repository for a newer commit.",
                # Refused while no worker would take it: the problem drawn on
                # the page says how to set one up.
                disabled=bool(self.untaken),
            )
            if refresh is not None
            else None
        )
        return (*decisions(self.sources.pending, self.principal, diff=True), *((asked,) if asked else ()))

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        sources = self.sources
        context.update(
            page_aside="control_plane/_playbooks_read.html",
            # View diff, Adopt all and Reject all, then Refresh, side by side while a commit waits.
            page_beside=MOST_BESIDE if sources.pending else "",
            failed_tone=(
                TONES[JobState(sources.reached.seen)]
                if sources.reached is not None and sources.reach.state == FAILED
                else ""
            ),
            sources=sources,
            # When HQ last reached the repository: a scan result is kept only by a read that succeeded.
            read_at=SourceProposal.objects.order_by("-pk").values_list("created_at", flat=True).first(),
            rows=playbook_rows(sources.procedures, sources.pending, self.principal),
            # HQ's playbook worker absent, with how to set it up: about no
            # record with a page, so found by its key.
            problems=worker_problems(self.principal),
        )
        return context


class RefreshView(AskMixin, View):
    """Ask HQ's runner to scan the repository now."""

    capability = REFRESH
    back_to = "control_plane:playbooks"

    @override
    def asked(self, result, at) -> Took:
        operation = str(result["operation"]["id"])
        return Took(scan_standing(operation) or Standing(), scan_status_url(operation))


class ScanStatusView(WatchView):
    """How a scan stands."""

    @override
    def standing(self, subject):
        return scan_standing(subject["scan"]) if isinstance(subject.get("scan"), str) else None


class ProposalDecisionView(View):
    """Adopt or reject one scan result, or one playbook's change in it,
    through its command, so a press is authorized and recorded as the same
    call through the API. A refusal goes back to the audit entry of the
    scan's read, which says what is wrong with the commit."""

    http_method_names: ClassVar[list[str]] = ["post"]
    capability: ClassVar[str]
    # The answer's field that says it changed anything, and what the next
    # page says when it did and when it did not.
    result_field: ClassVar[str]
    done: ClassVar[tuple[str, str]]

    def post(self, request, pk: int):
        # One playbook's row sends its key, and the page it was pressed on to return to.
        playbook = str(request.POST.get("playbook", ""))
        payload = {"proposal": pk, **({"playbook": playbook} if playbook else {})}
        result = execute_posted(request, self.capability, payload)
        if not result.get("ok") and refusal_of(result).code in WANTS_A_GRANT:
            raise PermissionDenied(refusal_of(result).message)
        if refused(request, result) is not None:
            scan = SourceProposal.objects.filter(pk=pk).first()
            return redirect(entry_url(scan) if scan else reverse("control_plane:playbooks"))
        changed, unchanged = self.done
        if result.get(self.result_field):
            messages.success(request, changed)
        else:
            messages.info(request, unchanged)
        return redirect(safe_next(request, fallback=reverse("control_plane:playbooks")))


class ProposalAdoptView(ProposalDecisionView):
    """Adopt one scan result's changes, or one playbook's: each becomes what its command is built from."""

    capability = ADOPT
    result_field = "adopted"
    done = ("Adopted.", "Already adopted.")


class ProposalRejectView(ProposalDecisionView):
    """Reject one scan result, or one playbook's change in it, so it waits no more."""

    capability = REJECT
    result_field = "rejected"
    done = ("Rejected.", "Already rejected.")

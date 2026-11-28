"""The Playbooks page: the adopted playbooks, their commit, and a newer scanned commit's changes awaiting a decision.

Every value is the catalog's or a scan's (``automation_sources.standing``), so
the host names no procedure, machine or repository.
"""

from functools import cached_property
from typing import ClassVar, override

from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect
from django.views import View
from django.views.generic import TemplateView

from hq.platform.application.automation_adoption import ADOPT, REJECT, scan_result
from hq.platform.application.automation_findings import worker_problems
from hq.platform.application.automation_sources import (
    SourceStanding,
    decisions,
    entry_url,
    playbook_table,
    standing,
)
from hq.platform.application.automation_workers import profile_workers, workers_table
from hq.platform.application.pages import PageMixin
from hq.platform.application.routes import reverse
from hq.platform.application.security import safe_next, web_principal
from hq.platform.application.web_writes import execute_posted
from hq.platform.core.web_refusals import refused
lazy from hq.domains.jobs.models import JobState
lazy from hq.platform.application.asks import FAILED
lazy from hq.platform.application.refusal_status import WANTS_A_GRANT, refusal_of
lazy from hq.platform.core.templatetags.action_tags import MOST_BESIDE


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
        return decisions(self.sources.pending, self.principal, diff=True)

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        sources = self.sources
        context.update(
            page_aside="control_plane/_playbooks_read.html",
            # View diff, Adopt all and Reject all side by side while a commit waits.
            page_beside=MOST_BESIDE if sources.pending else "",
            failed_tone=(
                JobState(sources.reached.seen).tone
                if sources.reached is not None and sources.reach.state == FAILED
                else ""
            ),
            sources=sources,
            # When HQ last reached the repository: a scan result is kept only by a read that succeeded.
            read_at=newest.created_at if (newest := scan_result()) else None,
            rows=playbook_table(sources.procedures, sources.pending, self.principal),
            # HQ's playbook worker absent, with how to set it up: about no
            # record with a page, so found by its key.
            problems=worker_problems(self.principal),
            workers=workers_table(profile_workers()),
        )
        return context


class ProposalDecisionView(View):
    """Adopt or reject one scan result, or one playbook's change in it,
    through its command, so a press is authorized and recorded as the same
    call through the API. A refusal goes back to the audit entry of the
    scan's read, which says what is wrong with the commit."""

    http_method_names: ClassVar[list[str]] = ["post"]
    capability: ClassVar[str]

    @property
    def runs(self) -> tuple[str, ...]:
        return (self.capability,)

    def post(self, request, pk: int):
        # One playbook's row sends its key, and the page it was pressed on to return to.
        playbook = str(request.POST.get("playbook", ""))
        payload = {"proposal": pk, **({"playbook": playbook} if playbook else {})}
        result = execute_posted(request, self.capability, payload)
        if not result.get("ok") and refusal_of(result).code in WANTS_A_GRANT:
            raise PermissionDenied(refusal_of(result).message)
        if refused(request, result) is not None:
            scan = scan_result(pk)
            return redirect(entry_url(scan) if scan else reverse("control_plane:playbooks"))
        return redirect(safe_next(request, fallback=reverse("control_plane:playbooks")))


class ProposalAdoptView(ProposalDecisionView):
    """Adopt one scan result's changes, or one playbook's: each becomes what its command is built from."""

    capability = ADOPT


class ProposalRejectView(ProposalDecisionView):
    """Reject one scan result, or one playbook's change in it, so it waits no more."""

    capability = REJECT

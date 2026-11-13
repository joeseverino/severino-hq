"""Where a job says what it is doing, and where they are all listed."""

from functools import cached_property
from typing import override

from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from django.urls import reverse
from django.views.generic import DetailView, ListView, View

from hq.platform.application.pages import PageAction, PageMixin
from hq.platform.application.security import web_principal
from hq.platform.application.tables import Projected, TableColumn, TableFilter, TableListMixin, TableQuery, TableSort
from hq.platform.application.web_writes import execute_posted
lazy from hq.platform.application import outbound_work, work_runs
lazy from hq.platform.application.asks import FAILED, Standing, answer, job_standing
lazy from hq.platform.application.automation_runs import listed_with_actions
lazy from hq.platform.application.labels import lower_first
lazy from hq.platform.application.outbound_work import SUBJECT_FIELD
lazy from hq.platform.application.refusal_status import WANTS_A_GRANT

from .models import Job, JobState


class JobListView(PageMixin, TableListMixin, ListView):
    """All the work HQ started, its own jobs and the automation runs it
    queued, as one list through the host's table contract (``work_runs``)."""

    table_empty = "No jobs yet."

    # The rows are the projection's; a job is what an estate fills it with.
    model = Job
    template_name = "jobs/job_list.html"
    paginate_by = 40
    page_title = "Background jobs"
    table_search_placeholder = "Search jobs and results…"
    table_columns = (
        TableColumn("Job", kind="prose"),
        TableColumn("State", css="key-col", kind="status"),
        TableColumn("Started", css="date-col"),
        TableColumn("Duration", kind="numeric"),
        TableColumn("Result", kind="prose"),
        TableColumn("Requested by", kind="prose"),
        TableColumn("", css="actions-col"),
    )
    # Each sort's value is the projection's order, which reads it.
    table_sorts = (
        TableSort(work_runs.FAILED_FIRST, "Failed first", work_runs.FAILED_FIRST),
        TableSort(work_runs.NEWEST, "Newest first", work_runs.NEWEST),
        TableSort(work_runs.OLDEST, "Oldest first", work_runs.OLDEST),
        TableSort(work_runs.BY_TITLE, "Job A–Z", work_runs.BY_TITLE),
        TableSort(work_runs.BY_STATE, "State", work_runs.BY_STATE),
    )
    table_default_sort = work_runs.FAILED_FIRST

    @override
    def get_page_actions(self):
        # What a job changed is recorded there.
        return (PageAction("Audit log", reverse("core:audit_list")),)

    @cached_property
    def kinds(self) -> list[work_runs.WorkKind]:
        """Each kind of work by its latest run: the Job filter's options and what failed last."""

        return work_runs.work_kinds()

    @override
    def get_table_filters(self):
        # Kinds are strings extensions and procedures choose, so the options
        # are whatever has actually run rather than a list this app keeps.
        # A machine is offered by its name where a run acted on it, and a
        # machine page links here with it chosen.
        return (
            TableFilter("state", "State", "state", JobState.choices),
            TableFilter("kind", "Job", "kind", [(found.kind, found.title) for found in self.kinds]),
            TableFilter("host", "Host", "host", [(host, host) for host in work_runs.run_hosts()]),
        )

    @override
    def get_table_rows(self, query: TableQuery) -> Projected:
        rows = work_runs.work_rows(
            state=query.chosen("state"),
            kind=query.chosen("kind"),
            host=query.chosen("host"),
            search=query.search,
            order=query.sort,
        )
        principal = web_principal(self.request.user)
        return Projected(rows, lambda page: listed_with_actions(work_runs.hydrated(page), principal))

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # What failed or stopped on its last run, which a later run of the same work has not got past,
        # each state's work under its own word when both are there.
        ended = {
            state: [found.title for found in self.kinds if found.state == state] for state in work_runs.ENDED_BADLY
        }
        context["ended_badly"] = [(JobState(state).label, titles) for state, titles in ended.items() if titles]
        if context["ended_badly"]:
            (first, _), *rest = context["ended_badly"]
            said = " or ".join((first, *(lower_first(label) for label, _ in rest)))
            many = sum(len(titles) for _, titles in context["ended_badly"]) > 1
            context["ended_badly_title"] = f"{said} on {'their' if many else 'its'} last run"
        return context


class JobStatusView(DetailView):
    """One job's state, as JSON, for a page that is watching it.

    Polled every couple of seconds for as long as the page is open, so it
    reads one row and renders no template. It answers as ``asks.Standing``
    does, so the control that follows a controller's read follows a job.
    """

    model = Job

    @override
    def render_to_response(self, context, **response_kwargs):
        job = self.object
        # The shape every ask's status answers in, with the job's own result.
        return JsonResponse({**job_standing(job).as_json(), "result": job.result})


class WorkAskView(View):
    """Where a control asks for declared outbound work.

    The ask is the work's own capability, so a pressed button is authorized,
    refused and recorded exactly as the same ask through the API is. The
    request answers once the job is recorded; the job does the work.
    """

    def post(self, request, name: str):
        work = outbound_work.declared_work().get(name)
        if work is None:
            raise Http404("No such work is declared.")
        subject = request.POST.get(SUBJECT_FIELD, "")
        result = execute_posted(request, name, {}, target=subject if work.takes_subject else None)
        back = reverse("jobs:list")
        if not result.get("ok"):
            error = result.get("error", {})
            if error.get("code") in WANTS_A_GRANT:
                raise PermissionDenied(error.get("message", ""))
            return answer(request, Standing(FAILED, error.get("message", "")), fallback=back)
        job = Job.objects.filter(pk=result["job"]).first() if result.get("job") else None
        if job is None:
            # Another subject's run of the same work holds the one live slot.
            return answer(request, Standing(FAILED, result.get("message", "")), fallback=back)
        return answer(
            request,
            job_standing(job),
            fallback=back,
            status_url=reverse("jobs:status", args=[job.pk]),
            message=result.get("message", ""),
        )

"""Where a job says what it is doing, and where they are all listed."""

from datetime import timedelta
from functools import cached_property
from typing import override

from django.http import Http404, JsonResponse
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.views.generic import DetailView, TemplateView, View

from hq.platform.application.list_pages import ListPageMixin
from hq.platform.application.pages import PageAction, PageBadge, RecordPageMixin, RouteNamed
from hq.platform.application.security import web_principal
from hq.platform.application.tables import Cell, Row, TableColumn
from hq.platform.application.tone import Tone
lazy from hq.platform.application import outbound_work, work_runs
lazy from hq.platform.application.asks import AskMixin
lazy from hq.platform.application.automation_runs import AGAIN, cancel_action
lazy from hq.platform.application.capabilities import capability_spec
lazy from hq.platform.application.labels import lower_first
lazy from hq.platform.application.moments import duration, moment
lazy from hq.platform.application.outbound_work import SUBJECT_FIELD
lazy from hq.platform.application.run_pages import run_context
lazy from hq.platform.application.run_reads import evidence_of
lazy from hq.platform.application.work_runs import RUN, operation_of

from .models import Job, JobState


def _ran_for(seconds: float | None) -> str:
    """How long a piece of work ran, from the one owner of a run time; "" when unknown."""

    return "" if seconds is None else duration(timedelta(seconds=seconds), short=True)


# A run's tone (``JobState``'s) in the notice's own names for it.
NOTICE_TONES = {Tone.SERIOUS: "error", Tone.ATTENTION: "warning"}


class JobListView(ListPageMixin, TemplateView):
    """All the work HQ started, its own jobs and the automation runs it
    queued, as one list: the ``work`` list read."""

    resource = "work"
    # The rows are the read's; a job is what an estate fills it with.
    model = Job
    table_empty = "No jobs yet."
    template_name = "jobs/job_list.html"
    page_size = 40
    page_title = "Background jobs"
    table_search_placeholder = "Search jobs and results…"
    table_columns = (
        TableColumn("Job", kind="prose", cell=lambda row: Cell(row["title"], row["url"], role="data")),
        TableColumn(
            "State", css="key-col", kind="status", cell=lambda row: Cell(row["state_label"], tone=str(row["tone"]))
        ),
        TableColumn("Started", cell=lambda row: Cell(moment=moment(row["created_at"]))),
        TableColumn("Duration", kind="numeric", cell=lambda row: Cell(_ran_for(row["duration_seconds"]))),
        TableColumn("Result", kind="prose", cell=lambda row: Cell(row["headline"] or "")),
        TableColumn("Requested by", kind="prose", cell=lambda row: Cell(row["by"], cut=bool(row["by"]))),
    )

    @override
    def table_row(self, row, cells):
        return Row(cells, actions=row["actions"], evidence=evidence_of(row) or None)

    @override
    def get_page_actions(self):
        # What a job changed is recorded there.
        return (PageAction("Audit log", reverse("core:audit_list")),)

    @override
    def row(self, item):
        # A run nothing has started ends with its Cancel button, for a reader who may cancel it.
        principal = web_principal(self.request.user)
        action = item["source"] == RUN and cancel_action(
            item["id"], operation_of(item["kind"]), item["state"], item["effect"], principal
        )
        # The read says an ended run's duration only, so a page drawn now says how long one has run so far.
        if item["state"] == JobState.RUNNING and (started := moment(item["started_at"])):
            item = {**item, "duration_seconds": (timezone.now() - started).total_seconds()}
        return {**item, "actions": (action,) if action else ()}

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # What failed or stopped on its last run, which a later run of the same work has not got past,
        # each state's work under its own word when both are there.
        kinds = work_runs.work_kinds()
        ended = {state: [found.title for found in kinds if found.state == state] for state in work_runs.ENDED_BADLY}
        context["ended_badly"] = [(JobState(state).label, titles) for state, titles in ended.items() if titles]
        if context["ended_badly"]:
            (first, _), *rest = context["ended_badly"]
            # The worst state leads ENDED_BADLY, and the banner reads in the tone its run does.
            worst = next(state for state, titles in ended.items() if titles)
            context["ended_badly_tone"] = NOTICE_TONES[JobState(worst).tone]
            said = " or ".join((first, *(lower_first(label) for label, _ in rest)))
            many = sum(len(titles) for _, titles in context["ended_badly"]) > 1
            context["ended_badly_title"] = f"{said} on {'their' if many else 'its'} last run"
        return context


class RunView(RecordPageMixin, TemplateView):
    """One run of a playbook, or one scan of its source: who asked, its
    approval, its lease, how it ended and each task's outcome, from its
    record (``application.run_reads``), beside what ``run_pages`` adds."""

    resource = "runs"
    context_object_name = "run"
    url_kwarg = "pk"
    template_name = "jobs/run_detail.html"
    page_trail = (("Background jobs", reverse_lazy("jobs:list")),)

    @property
    def following_run(self) -> bool:
        return self.record["state"] in {JobState.QUEUED, JobState.RUNNING}

    @override
    def get_page_title(self):
        return self.record["title"]

    @override
    def get_page_actions(self):
        run = self.record
        playbook = run["playbook"]
        cancel = cancel_action(
            run["operation_id"], playbook, run["state"], run["effect"], web_principal(self.request.user)
        )
        # Its playbook again as it was asked (``finding_runs.run_again``), once it has ended, while that playbook is adopted.
        again = (
            PageAction.command(run["capability"], variant=AGAIN, query={"again": str(run["operation_id"])})
            if not self.following_run and playbook and capability_spec(run["capability"])
            else None
        )
        return tuple(action for action in (again, cancel) if action is not None)

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["page_badges"] = (PageBadge(self.record["state_label"], Tone(self.record["tone"])),)
        context["following_run"] = self.following_run
        context.update(run_context(self.record, user=self.request.user))
        return context


class JobStatusView(DetailView):
    """How one job stands, for a page following it: one row, no template."""

    model = Job

    @override
    def render_to_response(self, context, **response_kwargs):
        return JsonResponse(work_runs.standing_of(work_runs.job_run(self.object)).as_json())


class RunStatusView(View):
    """How one run or scan stands, for a page following it: one row, no template."""

    def get(self, request, pk):
        found = work_runs.run_of(str(pk))
        if found is None:
            raise Http404("No run has that id.")
        return JsonResponse(work_runs.standing_of(found[0]).as_json())


class WorkAskView(AskMixin, View):
    """Where a control asks for declared outbound work, through the work's own
    capability, so a press is authorized and recorded as the API's call is."""

    back_to = "jobs:list"
    runs = RouteNamed()

    @override
    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.capability = kwargs["name"]

    @cached_property
    def work(self):
        work = outbound_work.declared_work().get(self.capability)
        if work is None or not work.offered:
            raise Http404("No such work is declared.")
        return work

    @override
    def target(self) -> str | None:
        return self.request.POST.get(SUBJECT_FIELD, "") if self.work.takes_subject else None

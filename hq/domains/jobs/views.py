"""Where a job says what it is doing, and where they are all listed."""

from functools import cached_property

from django.core.exceptions import PermissionDenied
from django.db.models import Case, IntegerField, Value, When
from django.http import Http404, JsonResponse
from django.urls import reverse
from django.views.generic import DetailView, ListView, View

from hq.platform.application.pages import PageMixin
from hq.platform.application.tables import TableColumn, TableFilter, TableListMixin, TableSort

from .models import Job
from .runner import reap


FAILED_FIRST = "failed"
ENDED_BADLY = (Job.State.FAILED, Job.State.LOST)


class JobListView(PageMixin, TableListMixin, ListView):
    """Every job, newest first, through the host's own table contract."""

    model = Job
    template_name = "jobs/job_list.html"
    paginate_by = 40
    page_title = "Background jobs"
    table_search_fields = ("label", "kind", "note")
    table_search_placeholder = "Search jobs and results…"
    page_lede = (
        "Work HQ does on a schedule or that you started, and how each run went. "
        "What changed as a result is in the audit log."
    )
    table_columns = (
        TableColumn("Started", "created_at"),
        TableColumn("Job", css="key-col"),
        TableColumn("State", css="key-col"),
        TableColumn("Duration", css="num-col"),
        TableColumn("Result"),
        TableColumn("Requested by"),
    )
    table_sorts = (
        # The order ``get_queryset`` gives it: what failed on its last run, then newest.
        TableSort(FAILED_FIRST, "Failed first", "-created_at"),
        TableSort("-created_at", "Newest first", "-created_at"),
        TableSort("created_at", "Oldest first", "created_at"),
        TableSort("kind", "Job A–Z", ("label", "-created_at")),
        TableSort("state", "State", ("state", "-created_at")),
    )
    table_default_sort = FAILED_FIRST

    @cached_property
    def latest_runs(self) -> dict[str, tuple[object, str, str]]:
        """Each kind's latest run as ``(id, state, label)``, from one read.

        The filter's options and what failed last both come from it.
        """

        latest: dict[str, tuple[object, str, str]] = {}
        for kind, pk, state, label in Job.objects.order_by("kind", "-created_at").values_list(
            "kind", "pk", "state", "label"
        ):
            latest.setdefault(kind, (pk, state, label or kind))
        return latest

    @property
    def failed_last(self) -> list[tuple[object, str]]:
        """``(id, label)`` of each job whose latest run failed or was interrupted.

        A failure a later run of the same job got past is history, and is not here.
        """

        return sorted(
            ((pk, label) for pk, state, label in self.latest_runs.values() if state in ENDED_BADLY),
            key=lambda found: found[1].casefold(),
        )

    def get_table_filters(self):
        # Kinds are strings extensions choose, so the options are whatever
        # has actually run rather than a list this app maintains. Each is
        # offered by the name its rows carry: its latest run's label.
        named = {kind: label for kind, (_pk, _state, label) in self.latest_runs.items()}
        return (
            TableFilter("state", "State", "state", Job.State.choices),
            TableFilter("kind", "Job", "kind", sorted(named.items(), key=lambda item: item[1].casefold())),
        )

    def get_queryset(self):
        # Anything that died is settled before the list is drawn, so the page
        # never shows a job as running when its process is gone. Off a page
        # view rather than a schedule, deliberately.
        reap()
        rows = self.apply_table_query(super().get_queryset().select_related("requested_by"))
        failed = [pk for pk, _label in self.failed_last]
        if failed and self.request.GET.get("sort", FAILED_FIRST) == FAILED_FIRST:
            rows = rows.order_by(
                Case(When(pk__in=failed, then=Value(0)), default=Value(1), output_field=IntegerField()),
                "-created_at",
                "pk",
            )
        return rows

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["failed_last"] = [label for _pk, label in self.failed_last]
        return context


class JobStatusView(DetailView):
    """One job's state, as JSON, for a page that is watching it.

    Polled every couple of seconds for as long as the page is open, so it
    reads one row and renders no template. It answers as ``asks.Standing``
    does, so the control that follows a controller's read follows a job.
    """

    model = Job

    def render_to_response(self, context, **response_kwargs):
        job = self.object
        if job.is_stale:
            reap(job.kind)
            job.refresh_from_db()
        from hq.platform.application.asks import job_standing

        # The shape every ask's status answers in, with the job's own result.
        return JsonResponse({**job_standing(job).as_json(), "result": job.result})


class WorkAskView(View):
    """Where a control asks for declared outbound work.

    The ask is the work's own capability, so a pressed button is authorized,
    refused and recorded exactly as the same ask through the API is. The
    request answers once the job is recorded; the job does the work.
    """

    def post(self, request, name: str):
        from hq.platform.application.asks import FAILED, Standing, answer, job_standing
        from hq.platform.application.capabilities import execute_capability
        from hq.platform.application.outbound_work import SUBJECT_FIELD, declared_work
        from hq.platform.application.security import web_principal

        work = declared_work().get(name)
        if work is None:
            raise Http404("No such work is declared.")
        subject = request.POST.get(SUBJECT_FIELD, "")
        result = execute_capability(
            name,
            {},
            principal=web_principal(request.user),
            target=subject if work.takes_subject else None,
        )
        back = reverse("jobs:list")
        if not result.get("ok"):
            error = result.get("error", {})
            if error.get("code") == "forbidden":
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

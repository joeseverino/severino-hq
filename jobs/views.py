"""Where a job says what it is doing, and where they are all listed."""

from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.views.generic import DetailView, ListView

from application.tables import TableFilter, TableListMixin, TableSort

from .models import Job
from .runner import reap


class JobListView(TableListMixin, LoginRequiredMixin, ListView):
    """Every job, newest first, through the host's own table contract."""

    model = Job
    template_name = "jobs/job_list.html"
    context_object_name = "jobs"
    paginate_by = 40
    table_sorts = (
        TableSort("-created_at", "Newest first", "-created_at"),
        TableSort("created_at", "Oldest first", "created_at"),
        TableSort("kind", "Kind A–Z", ("kind", "-created_at")),
        TableSort("state", "State", ("state", "-created_at")),
    )
    table_default_sort = "-created_at"

    def get_table_filters(self):
        # Kinds are strings extensions choose, so the options are whatever
        # has actually run rather than a list this app maintains.
        kinds = (
            Job.objects.order_by("kind")
            .values_list("kind", flat=True)
            .distinct()
        )
        return (
            TableFilter("state", "State", "state", Job.State.choices),
            TableFilter("kind", "Kind", "kind", [(kind, kind) for kind in kinds]),
        )

    def get_queryset(self):
        # Anything that died is settled before the list is drawn, so the page
        # never shows a job as running when its process is gone. Off a page
        # view rather than a schedule, deliberately.
        reap()
        return self.apply_table_query(
            super().get_queryset().select_related("requested_by")
        )


class JobStatusView(LoginRequiredMixin, DetailView):
    """One job's state, as JSON, for a page that is watching it.

    Polled every couple of seconds for as long as the page is open, so it
    reads one row and renders no template.
    """

    model = Job

    def render_to_response(self, context, **response_kwargs):
        job = self.object
        if job.is_stale:
            reap(job.kind)
            job.refresh_from_db()
        return JsonResponse(
            {
                "state": job.state,
                "label": job.get_state_display(),
                "percent": job.percent,
                "note": job.note,
                "live": job.is_live,
                "seconds": round(job.duration_seconds or 0),
                "result": job.result,
                # The last line only. The whole traceback is on the job and
                # in the audit log; a progress panel wants the sentence.
                "error": job.error.strip().splitlines()[-1] if job.error else "",
            }
        )

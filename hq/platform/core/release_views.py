"""HQ's releases, and one of them whole beside the release before it."""

from typing import override

from django.urls import reverse_lazy
from django.views.generic import TemplateView

from hq.platform.application.list_pages import ListPageMixin
from hq.platform.application.moments import moment
from hq.platform.application.pages import RecordPageMixin
from hq.platform.application.releases import RELEASES, comparison
from hq.platform.application.tables import Cell, TableColumn
lazy from hq.platform.application import deploy_outcomes, release_pages

from .models import ReleaseSnapshot


def _outcome(row) -> Cell:
    return Cell(row["outcome_said"], tone=str(deploy_outcomes.said(row)[1]))


class ReleaseListView(ListPageMixin, TemplateView):
    """HQ's releases, newest first: the ``releases`` list read."""

    resource = RELEASES
    # The rows are the GitHub reading's; a kept snapshot is the one row of
    # HQ's own a derived page can fill.
    model = ReleaseSnapshot
    template_name = "list_page.html"
    page_title = "Releases"
    page_trail = (("HQ", reverse_lazy("hq_self")),)
    table_empty = "No release read yet."
    table_empty_remedy = False
    table_columns = (
        TableColumn("Commit", kind="identifier", cell=lambda row: Cell(row["commit"][:7], row["url"], readout=True)),
        TableColumn("Outcome", kind="status", css="key-col", cell=_outcome),
        TableColumn("Approved by", kind="prose", cell=lambda row: Cell(row["approved_by"])),
        TableColumn("Deployed", cell=lambda row: Cell(moment=moment(row["ended_at"] or row["created_at"]))),
    )


class ReleaseView(RecordPageMixin, TemplateView):
    """One release: its commit, how it ended, its run, who approved it, and
    what HQ read of itself once it went live beside the release before it."""

    resource = RELEASES
    context_object_name = "release"
    template_name = "core/release_detail.html"
    page_trail = (("HQ", reverse_lazy("hq_self")), ("Releases", reverse_lazy("releases:list")))

    @override
    def get_page_title(self) -> str:
        return f"Release {self.record['commit'][:7]}"

    @override
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["tone"] = deploy_outcomes.said(self.record)[1]
        context["readings"] = comparison(self.record)
        context["pages"] = release_pages.table(self.record)
        return context

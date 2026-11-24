"""The errors HQ kept of the requests it failed, and one of them whole."""

from typing import override

from django.urls import reverse_lazy
from django.views.generic import TemplateView

from hq.platform.application.error_reads import SERVER_ERRORS
from hq.platform.application.list_pages import ListPageMixin
from hq.platform.application.moments import moment
from hq.platform.application.pages import RecordPageMixin
from hq.platform.application.security import Capability
from hq.platform.application.tables import Cell, TableColumn

from .models import ServerError


def _short(exception: str) -> str:
    """An exception by its class alone, as a row names it; its page says the module."""

    return exception.rpartition(".")[2]


class ServerErrorListView(ListPageMixin, TemplateView):
    """The errors kept, newest first: the ``server_errors`` list read."""

    # A traceback can carry configuration and data: read as the audit log is.
    required_capability = Capability.READ_AUDIT_LOG
    resource = SERVER_ERRORS
    # The rows are the read's; a kept error is what a derived page fills it with.
    model = ServerError
    template_name = "list_page.html"
    page_title = "Errors"
    page_size = 50
    table_empty = "No errors kept."
    table_empty_remedy = False
    table_columns = (
        TableColumn("Error", kind="prose", cell=lambda row: Cell(_short(row["exception"]), row["url"], role="data")),
        TableColumn("Message", kind="prose", cell=lambda row: Cell(row["message"], cut=bool(row["message"]))),
        TableColumn("Path", kind="prose", cell=lambda row: Cell(f"{row['method']} {row['path']}".strip(), cut=True)),
        TableColumn("By", kind="prose", cell=lambda row: Cell(row["by"])),
        TableColumn("When", sort="occurred_at", cell=lambda row: Cell(moment=moment(row["occurred_at"]))),
    )


class ServerErrorView(RecordPageMixin, TemplateView):
    """One error whole: where and for whom it failed, what was raised, and its traceback."""

    required_capability = Capability.READ_AUDIT_LOG
    resource = SERVER_ERRORS
    context_object_name = "error"
    template_name = "core/server_error_detail.html"
    page_trail = (("Errors", reverse_lazy("errors:list")),)

    @override
    def get_page_title(self) -> str:
        return _short(self.record["exception"])

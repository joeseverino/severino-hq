"""For an extension's list page: one page of its resource's list read, drawn in the columns it declares."""

from hq.platform.application.list_pages import ListPageMixin
from hq.platform.application.page_reads import Remedy
from hq.platform.application.tables import TableColumn

__all__ = ["ListPageMixin", "Remedy", "TableColumn"]

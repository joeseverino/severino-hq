"""For an extension's tables: a list page of its resource's list read, drawn in the columns it
declares, and any other table declared whole (``DataTable``) for ``{% data_table %}`` to draw, its
entity cells named by the host's link builders (``Cell.of(record_link(row))``)."""

from hq.platform.application.entity_links import EntityLink, declared_link, record_link
from hq.platform.application.list_pages import ListPageMixin
from hq.platform.application.page_reads import Remedy
from hq.platform.application.tables import Below, Cell, DataTable, Group, Row, RowEditor, StatusMark, TableColumn

__all__ = [
    "Below",
    "Cell",
    "DataTable",
    "EntityLink",
    "Group",
    "ListPageMixin",
    "Remedy",
    "Row",
    "RowEditor",
    "StatusMark",
    "TableColumn",
    "declared_link",
    "record_link",
]

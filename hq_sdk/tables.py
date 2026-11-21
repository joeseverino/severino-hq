"""For an extension's tables: a list page of its resource's list read, drawn in the columns it
declares, and any other table declared whole (``DataTable``) for ``{% data_table %}`` to draw."""

from hq.platform.application.list_pages import ListPageMixin
from hq.platform.application.page_reads import Remedy
from hq.platform.application.tables import Cell, DataTable, Group, Row, RowEditor, TableColumn

__all__ = ["Cell", "DataTable", "Group", "ListPageMixin", "Remedy", "Row", "RowEditor", "TableColumn"]

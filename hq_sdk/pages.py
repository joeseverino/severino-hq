"""The page frame, for extension views.

A page declares its head (PageMixin, or page_context for a function view) and
extends ``page.html``, which draws its actions and their dialogs; a list extends
``list_page.html`` and writes only one row's cells (docs/Plugins.md, Shared UI
contract). An action is a ``PageAction`` or an ``Ask``, what ``outbound.ask``
returns. ``built_from`` links an extension's overview to its project.
"""

from hq.platform.application.asks import Ask
from hq.platform.application.pages import Crumb, Page, PageAction, PageDialog, PageMixin, page_context
from hq.platform.application.projects import built_from

__all__ = ["Ask", "Crumb", "Page", "PageAction", "PageDialog", "PageMixin", "built_from", "page_context"]

"""The page frame, for extension views (docs/Plugins.md, Shared UI contract).

A page declares its head (PageMixin, or page_context) and extends
``page.html``; a list extends ``list_page.html``. An action is a
``PageAction``, a command's control from ``PageAction.command``, or an ``Ask``.
``built_from`` links an overview to its project.
"""

from hq.platform.application.asks import Ask
from hq.platform.application.pages import Crumb, Page, PageAction, PageDialog, PageMixin, page_context
from hq.platform.application.projects import built_from

__all__ = ["Ask", "Crumb", "Page", "PageAction", "PageDialog", "PageMixin", "built_from", "page_context"]

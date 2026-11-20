"""The page frame, for extension views (docs/Plugins.md, Shared UI contract).

A page declares its head (PageMixin, or page_context) and extends
``page.html``; a list extends ``list_page.html``. An action is a
``PageAction``, or a command's control from ``PageAction.command``.
``built_from`` links an overview to its project.
"""

from hq.platform.application.pages import Crumb, PageAction, PageDialog, PageMixin, page_context
from hq.platform.application.projects import built_from

__all__ = ["Crumb", "PageAction", "PageDialog", "PageMixin", "built_from", "page_context"]

"""The page frame, for extension views.

A page declares its head (PageMixin, or page_context for a function view) and
extends ``page.html``. A list also uses hq_sdk.tables and extends
``list_page.html``, writing only the cells of one row. See application.pages.

``built_from`` answers an extension's id with a link to the project it is
built from, for its overview to show.
"""

from hq.platform.application.pages import Page, PageAction, PageMixin, page_context
from hq.platform.application.projects import built_from

__all__ = ["Page", "PageAction", "PageMixin", "built_from", "page_context"]

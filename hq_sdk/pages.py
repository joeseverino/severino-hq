"""The page frame, for extension views.

A page declares its head (PageMixin, or page_context for a function view) and
extends ``page.html``. A list also uses hq_sdk.tables and extends
``list_page.html``, writing only the cells of one row. See application.pages.
"""

from application.pages import Page, PageAction, PageMixin, page_context

__all__ = ["Page", "PageAction", "PageMixin", "page_context"]

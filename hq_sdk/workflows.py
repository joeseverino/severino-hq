"""For a link an extension offers on a page or a problem: to a page, or to an HQ command.

A link that only opens a page is ``read_link``'s, which gives it no effect or command of its own.
"""

from hq.platform.application.action_links import ActionLink, read_link

__all__ = ["ActionLink", "read_link"]

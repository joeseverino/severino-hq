"""The frame every page shares: its head, its actions, its section navigation.

A view says what its page is (a title, a lede, the actions it offers) and
``templates/page.html`` draws it, the same way on every page, host or
extension. A page template fills only ``{% block page %}``. It never writes its
own head, so no two heads can differ, and it never writes a form for one
button: a post action is a ``post_button``.

Class-based views mix in ``PageMixin``; function views pass ``page_context``
to ``render``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ui import PageNavigation


@dataclass(frozen=True)
class PageAction:
    """One control in a page's head.

    A GET action is a link, and may open a dialog on the page (``modal``) while
    still pointing at a page that does the same job. A POST action is a
    ``post_button``, so it needs no form of its own.
    """

    label: str
    url: str
    method: str = "get"
    primary: bool = False
    danger: bool = False
    modal: str = ""
    value: str = ""
    title: str = ""
    # Shown but not usable, with ``title`` saying why.
    disabled: bool = False

    def __post_init__(self):
        if self.method not in {"get", "post"}:
            raise ValueError(f"PageAction method must be get or post, not {self.method!r}.")
        if self.modal and self.method != "get":
            raise ValueError("Only a GET action can open a dialog.")

    @property
    def css(self) -> str:
        return " ".join(
            (
                "btn",
                *(("primary",) if self.primary else ()),
                # Quiet: a head's destructive action is the rarest thing on the
                # page and should not be its loudest. Its confirmation is loud.
                *(("ghost", "danger") if self.danger else ()),
            )
        )


@dataclass(frozen=True)
class Page:
    title: str
    lede: str = ""
    actions: tuple[PageAction, ...] = ()
    navigation: PageNavigation | None = None
    # Where the page sits, as (label, url) pairs, when it is reached from a parent.
    trail: tuple[tuple[str, str], ...] = field(default_factory=tuple)


def page_context(
    title: str,
    lede: str = "",
    *,
    actions=(),
    navigation: PageNavigation | None = None,
    trail=(),
) -> dict[str, Page]:
    """``{"page": Page(...)}``, for a function view's ``render`` context."""
    return {
        "page": Page(
            title=title,
            lede=lede,
            actions=tuple(actions),
            navigation=navigation,
            trail=tuple(trail),
        )
    }


def record_trail(list_crumb: tuple[str, str], record, label) -> tuple[tuple[str, str], ...]:
    """A list page's crumb, then the record's own when there is one."""

    crumbs = [list_crumb]
    if record is not None:
        crumbs.append((label(record), record.get_absolute_url()))
    return tuple(crumbs)


class PageMixin:
    """Declare a page's head on the view; ``page.html`` renders it."""

    page_title = ""
    page_lede = ""

    def get_page_title(self) -> str:
        return self.page_title

    def get_page_lede(self) -> str:
        return self.page_lede

    def get_page_actions(self) -> tuple[PageAction, ...]:
        return ()

    def get_page_navigation(self) -> PageNavigation | None:
        return None

    def get_page_trail(self) -> tuple[tuple[str, str], ...]:
        return ()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            page_context(
                self.get_page_title(),
                self.get_page_lede(),
                actions=self.get_page_actions(),
                navigation=self.get_page_navigation(),
                trail=self.get_page_trail(),
            )
        )
        return context

"""The frame every page shares: its head, its actions, its section navigation.

A view says what its page is (a title, a lede, the actions it offers) and
``templates/page.html`` draws it, the same way on every page, host or
extension. A page template fills only ``{% block page %}``. It never writes its
own head, so no two heads can differ, and it never writes a form for one
button: a post action is a ``post_button``.

Class-based views mix in ``PageMixin``; function views pass ``page_context``
to ``render``.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .fragments import FragmentMixin
from .ui import PageNavigation
lazy from .page_reads import remedy

PAGE_DIALOG_ID = re.compile(r"[a-z][a-z0-9-]*\Z")


@dataclass(frozen=True, slots=True)
class PageDialog:
    """A dialog a head action opens, drawn once by ``page.html``.

    ``body_template`` is rendered with the page's own context. The action's
    link still reaches a page that does the same job, so the control works
    when the dialog does not open.
    """

    id: str
    title: str
    body_template: str
    lede: str = ""

    def __post_init__(self):
        if not PAGE_DIALOG_ID.match(self.id):
            raise ValueError(f"A dialog id is lowercase words joined by hyphens, not {self.id!r}.")


@dataclass(frozen=True, slots=True)
class PageAction:
    """One control in a page's head.

    A GET action is a link, and may open a dialog (``modal``) while still
    pointing at a page that does the same job: a ``PageDialog`` the page draws,
    or the id of one the host already draws on every page. A POST action is a
    ``post_button``, so it needs no form of its own.
    """

    label: str
    url: str
    method: str = "get"
    primary: bool = False
    danger: bool = False
    modal: PageDialog | str = ""
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
    def dialog(self) -> PageDialog | None:
        """The dialog this action brings to the page, if it brings one."""

        return self.modal if isinstance(self.modal, PageDialog) else None

    @property
    def modal_id(self) -> str:
        """The id the link opens, wherever the dialog is drawn."""

        return self.modal.id if isinstance(self.modal, PageDialog) else self.modal

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


@dataclass(frozen=True, slots=True)
class PageBadge:
    """A short fact beside a page's title: its reach, that it is read-only."""

    label: str
    tone: str = ""
    title: str = ""


@dataclass(frozen=True, slots=True)
class Page:
    title: str
    lede: str = ""
    actions: tuple[PageAction, ...] = ()
    navigation: PageNavigation | None = None
    # Where the page sits, as (label, url) pairs, when it is reached from a parent.
    trail: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    # What the page shows, by the names it is declared under
    # (``application.page_reads``).
    reads: tuple[str, ...] = ()

    @property
    def remedy(self) -> Any:
        """What would fill the page while nothing has been read: the connection
        that reads what it shows, else its own lead action. Asked only by an
        empty state, so a full page never looks."""

        return (remedy(self.reads) if self.reads else None) or self.lead

    @property
    def lead(self) -> PageAction | None:
        """The page's own way to add what it lists: its primary link."""

        return next((action for action in self.actions if action.primary and action.method == "get"), None)

    @property
    def dialogs(self) -> tuple[PageDialog, ...]:
        """Every dialog the head's actions open, each drawn once."""

        found = (getattr(action, "dialog", None) for action in self.actions)
        return tuple(dict.fromkeys(dialog for dialog in found if dialog is not None))


def page_context(
    title: str,
    lede: str = "",
    *,
    actions: Iterable[PageAction] = (),
    navigation: PageNavigation | None = None,
    trail: Iterable[tuple[str, str]] = (),
    reads: Iterable[str] = (),
) -> dict[str, Page]:
    """``{"page": Page(...)}``, for a function view's ``render`` context."""
    return {
        "page": Page(
            title=title,
            lede=lede,
            actions=tuple(actions),
            navigation=navigation,
            trail=tuple(trail),
            reads=tuple(reads),
        )
    }


def record_trail(list_crumb: tuple[str, str], record, label) -> tuple[tuple[str, str], ...]:
    """A list page's crumb, then the record's own when there is one."""

    crumbs = [list_crumb]
    if record is not None:
        crumbs.append((label(record), record.get_absolute_url()))
    return tuple(crumbs)


class PageMixin(FragmentMixin):
    """Declare a page's head on the view; ``page.html`` renders it.

    A page answers one named part of itself to a request that asks for it
    (`hq.platform.application.fragments`).
    """

    page_title = ""
    page_lede = ""
    # What the page shows (``application.page_reads``), so an empty one can
    # name the connection that would fill it.
    page_reads: tuple[str, ...] = ()

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

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context.update(
            page_context(
                self.get_page_title(),
                self.get_page_lede(),
                actions=self.get_page_actions(),
                navigation=self.get_page_navigation(),
                trail=self.get_page_trail(),
                reads=self.page_reads,
            )
        )
        return context

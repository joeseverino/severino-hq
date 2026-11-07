"""The frame every page shares: its head, its actions, its section navigation.

A view says what its page is (a title, a lede, the actions it offers) and
``templates/page.html`` draws it the same way on every page, host or extension.
A page template fills only ``{% block page %}``, so it never writes its own head
or a form for one button (a ``post_button``). Class-based views mix in
``PageMixin``; function views pass ``page_context`` to ``render``.

Every page also reads under one capability, checked before its view runs
(``page_refusal``, docs/Security.md#what-a-page-reads-under).
"""

import re
from collections.abc import Callable, Iterable
from copy import copy
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit
from weakref import WeakKeyDictionary

from django.core.exceptions import ImproperlyConfigured
from django.urls import Resolver404, get_resolver, get_script_prefix, get_urlconf, resolve
from django.utils.functional import Promise

from .asks import Ask
from .fragments import FragmentMixin
from .ui import PageNavigation
lazy from .page_reads import remedy
lazy from .refusal_status import Refusal, refusal_of
lazy from .request_context import current as current_request
lazy from .request_identity import signed_in
lazy from .routes import reverse
lazy from .security import AuthorizationError, Capability, web_principal

PAGE_DIALOG_ID = re.compile(r"[a-z][a-z0-9-]*\Z")

# One step of a trail, (label, url); the url may be lazy, since a trail is
# declared on a class before the URLconf loads.
type Crumb = tuple[str, str | Promise]


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
class PageVisit:
    """What a page is about, as the readings it asks for when opened name it
    (``visit_refresh.SUBJECTS``). Context rather than a Page field, as
    ``page_badges`` is, so the SDK's Page is unchanged."""

    subject: str
    name: str


@dataclass(frozen=True, slots=True)
class Page:
    title: str
    lede: str = ""
    actions: tuple[PageAction | Ask, ...] = ()
    navigation: PageNavigation | None = None
    # Where the page sits, as (label, url) pairs, when it is reached from a parent.
    trail: tuple[Crumb, ...] = field(default_factory=tuple)
    # What the page shows, by the names it is declared under
    # (``application.page_reads``).
    reads: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # The head offers only the links its reader may follow.
        actions = tuple(each for each in self.actions if not isinstance(each, PageAction) or offered_now(each.url))
        object.__setattr__(self, "actions", actions)
        object.__setattr__(self, "trail", tuple(crumb for crumb in self.trail if offered_now(crumb[1])))

    @property
    def remedy(self) -> Any:
        """What would fill the page while nothing has been read: the connection
        that reads what it shows, else its own lead action. Asked only by an
        empty state, so a full page never looks."""

        return (remedy(self.reads) if self.reads else None) or self.lead

    @property
    def lead(self) -> PageAction | None:
        """The page's own way to add what it lists: its primary link."""

        links = (action for action in self.actions if isinstance(action, PageAction))
        return next((action for action in links if action.primary and action.method == "get"), None)

    @property
    def dialogs(self) -> tuple[PageDialog, ...]:
        """Every dialog the head's actions open, each drawn once."""

        found = (action.dialog for action in self.actions if isinstance(action, PageAction))
        return tuple(dict.fromkeys(dialog for dialog in found if dialog is not None))


def page_context(
    title: str,
    lede: str = "",
    *,
    actions: Iterable[PageAction | Ask] = (),
    navigation: PageNavigation | None = None,
    trail: Iterable[Crumb] = (),
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


def _is_trail(value: object) -> bool:
    """Whether ``value`` is crumbs: a tuple of (label, url) pairs."""

    return type(value) is tuple and all(
        type(crumb) is tuple and len(crumb) == 2 and isinstance(crumb[0], str) and isinstance(crumb[1], str | Promise)
        for crumb in value
    )


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
    # The pages above this one, from the top, as (label, url) crumbs; a
    # record's own crumb follows them when ``trail_label`` names it.
    page_trail: tuple[Crumb, ...] = ()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        # One crumb written bare, ``("Things", url)``, would draw each of its
        # halves as a crumb, so the shape is refused where the view is declared.
        super().__init_subclass__(**kwargs)
        if not _is_trail(cls.page_trail):
            raise ImproperlyConfigured(
                f"{cls.__name__}.page_trail is a tuple of (label, url) crumbs, not {cls.page_trail!r}."
            )

    def get_page_title(self) -> str:
        return self.page_title

    def get_page_lede(self) -> str:
        return self.page_lede

    def get_page_actions(self) -> tuple[PageAction | Ask, ...]:
        return ()

    def get_page_navigation(self) -> PageNavigation | None:
        return None

    def trail_label(self, record: Any) -> str | None:  # noqa: ARG002 - the default; an override is given the record
        """The record's crumb after ``page_trail``'s; None, the default, leaves it off.

        ``record`` is ``self.object``, the record as stored: an edit page's
        form changes a copy (``get_form_kwargs``).
        """

        return None

    def get_page_trail(self) -> tuple[Crumb, ...]:
        if not self.page_trail:
            return ()
        # A list page has no record of its own.
        record = getattr(self, "object", None)
        words = None if record is None else self.trail_label(record)
        if words is None:
            return self.page_trail
        return (*self.page_trail, (words, record.get_absolute_url()))

    def get_form_kwargs(self) -> dict[str, Any]:
        """An edit page's form changes a copy of its record, so the head reads
        the record as stored.

        A ``ModelForm`` writes what it cleaned onto its instance before it knows
        the whole form is valid (``BaseModelForm._post_clean``), and an edit
        view's instance is ``self.object``: a refused edit would title the page
        and link its trail by the values it refused.
        """

        kwargs = super().get_form_kwargs()
        if kwargs.get("instance") is not None:
            kwargs["instance"] = copy(kwargs["instance"])
        return kwargs

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


def page_capability(view: Callable[..., Any]) -> str:
    """The one capability the routed ``view`` reads under: the ``required_capability`` it declares, else READ.

    A class declares it as an attribute, a function view through
    ``hq_sdk.web.capability_required``. A page that declares nothing reads
    under READ, never under nothing; one that declares the attribute and
    leaves it empty (``hq_sdk.web.CapabilityRequiredMixin``'s default) is
    refused, since it meant to name one.
    """

    # A class view's attributes are its class's; a function view's are its own.
    owner: Any = vars(view).get("view_class", view)
    try:
        declared = owner.required_capability
    except AttributeError:
        return str(Capability.READ)
    if not isinstance(declared, str):
        raise ImproperlyConfigured(f"A page reads under one capability, not {declared!r}.")
    if not declared:
        raise ImproperlyConfigured(f"{owner.__qualname__!r} must define required_capability.")
    return str(declared)


def authorizes_itself(view: Callable[..., Any]) -> bool:
    """Whether the route ``view`` answers without a session, and so is no page this rule holds.

    The routes marked ``login_not_required`` (the machine API, health, sign-in
    and the identity provider's callbacks, CSP reports, the icons). Each
    refuses on its own terms: a token's grant, a signed logout token, or
    nothing to refuse.
    """

    return not getattr(view, "login_required", True)


def principal_opens(principal: Any, view: Callable[..., Any]) -> bool:
    """Whether ``principal`` may open the page ``view`` draws: the one place the page rule is written."""

    return principal.permits(page_capability(view))


def page_refusal(user: Any, view: Callable[..., Any]) -> Refusal | None:
    """Why the signed-in ``user`` may not open the page ``view`` draws, recorded, or None when they may.

    Asked before the view runs, so a refused page has read nothing.
    """

    principal = web_principal(user)
    if principal_opens(principal, view):
        return None
    capability = page_capability(view)
    refused = AuthorizationError(f"{principal.actor!r} holds no {capability!r}, which this page reads under.")
    return refusal_of(refused, name=capability, principal=principal)


def offered(user: Any | None, url: str | Promise) -> bool:
    """Whether HQ offers ``user`` a link to ``url``: a page of its own only while they may open it.

    ``user`` is the signed-in person, or None for nobody. Every link HQ offers by route (a nav item, a head action, a crumb) asks
    this, by the rule the page gate serves by, so a link is never offered to a
    page that would refuse its reader. A link elsewhere, or to a route that
    authorizes itself, is not HQ's to withhold.
    """

    return Offering(user)(url)


class Offering:
    """Which links HQ offers one ``user``, by address or by route name, under ``offered``'s rule.

    Their principal and the routes in force are read once, at the first page
    link each is asked for, since a page asks of every section, action and
    crumb it draws.
    """

    def __init__(self, user: Any | None) -> None:
        self.user = user
        self._principal: Any = None
        self._routes: dict[str, Callable[..., Any] | None] | None = None

    def __call__(self, url: str | Promise) -> bool:
        parts = urlsplit(str(url))
        if parts.scheme or parts.netloc:
            return True
        view = _routed(parts.path)
        return view is None or self.opens(view)

    def route(self, name: str) -> bool:
        if self._routes is None:
            self._routes = _route_views()
        view = _route_in(self._routes, name)
        return view is None or self.opens(view)

    def opens(self, view: Callable[..., Any]) -> bool:
        if authorizes_itself(view):
            return True
        if self.user is None:
            return False
        if self._principal is None:
            self._principal = web_principal(self.user)
        return principal_opens(self._principal, view)


def _routed(path: str) -> Callable[..., Any] | None:
    """The view HQ answers ``path`` with, or None where HQ routes nothing, which is not HQ's to withhold."""

    try:
        return resolve(path).func
    except Resolver404:
        return None


# The view each route name resolves to, by the resolver that holds it and the
# script prefix its address is reversed under: Django makes a new resolver
# whenever its URL caches are cleared, so a stale entry is never read, and route
# names are a fixed set, so this holds a URLconf's worth.
_ROUTE_VIEWS: WeakKeyDictionary[Any, dict[str, dict[str, Callable[..., Any] | None]]] = WeakKeyDictionary()


def _route_views() -> dict[str, Callable[..., Any] | None]:
    """The views kept for the URLconf and script prefix in force, by route name."""

    return _ROUTE_VIEWS.setdefault(get_resolver(get_urlconf()), {}).setdefault(get_script_prefix(), {})


def _route_in(views: dict[str, Callable[..., Any] | None], name: str) -> Callable[..., Any] | None:
    if name not in views:
        views[name] = _routed(urlsplit(str(reverse(name))).path)
    return views[name]


def offered_now(url: str | Promise) -> bool:
    """``offered`` to the person the request being answered is for.

    Outside a request there is nobody to ask, so nothing is withheld; the page
    gate still refuses whoever follows the link.
    """

    request = current_request()
    return request is None or offering_for(request)(url)


# One Offering for each request, so its reader's principal is built once for
# the nav, the head and every remedy the page draws, however many links each asks of.
_OFFERINGS: WeakKeyDictionary[Any, Offering] = WeakKeyDictionary()


def offering_for(request: Any) -> Offering:
    """The links HQ offers the person ``request`` is for, built again if a sign-in changed who that is."""

    user = signed_in(request)
    kept = _OFFERINGS.get(request)
    if kept is None or kept.user is not user:
        kept = _OFFERINGS[request] = Offering(user)
    return kept

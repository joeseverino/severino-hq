"""The frame every page shares: its head, its actions, its section navigation.

A view says what its page is and ``templates/page.html`` draws it the same way
on every page. Every page reads under one capability, checked before its view
runs (docs/Security.md#what-a-page-reads-under).
"""

import dataclasses
import re
from collections.abc import Callable, Iterable, Mapping
from copy import copy
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any
from urllib.parse import unquote, urlsplit
from weakref import WeakKeyDictionary

from django.core.exceptions import ImproperlyConfigured
from django.http import Http404
from django.shortcuts import redirect
from django.urls import NoReverseMatch, Resolver404, get_resolver, get_script_prefix, get_urlconf, resolve
from django.utils.functional import Promise
from django.utils.http import url_has_allowed_host_and_scheme

from .asks import Ask
from .fragments import FragmentMixin
from .tone import Tone
from .ui import PageNavigation, button_classes
from .url_query import with_query
lazy from .action_links import command_url
lazy from .capabilities import command_spec
lazy from .contracts import Effect
lazy from .domains import domain_navigation
lazy from .outbound_work import refusal as outbound_refusal
lazy from .page_reads import remedy
lazy from .record_errors import RecordNotFound
lazy from .refusal_status import Refusal, refusal_of
lazy from .request_context import current as current_request
lazy from .request_identity import signed_in
lazy from .resources import every_item, get_resource, resource_registry
lazy from .routes import reverse, routed
lazy from .security import AuthorizationError, Capability, web_principal

PAGE_DIALOG_ID = re.compile(r"[a-z][a-z0-9-]*\Z")

# One step of a trail, (label, url); the url may be lazy, since a trail is
# declared on a class before the URLconf loads.
type Crumb = tuple[str, str | Promise]
# What a control for a command acts on, and what is added to its address.
type Target = str | int | Mapping[str, str]
type Query = Mapping[str, str | tuple[str, ...]]


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
    """One control in a page's head or a row's end.

    A control for a command is built by ``command``, ``post`` or ``dialog``
    alone, from the declarations, so a hand-built one cannot post, look destructive or say
    words of its own.
    """

    label: str
    url: str
    primary: bool = False
    modal: PageDialog | str = ""
    title: str = ""
    # Shown but not usable, with ``title`` saying why.
    disabled: bool = False
    # The rest are a command's, set only by its constructors.
    method: str = dataclasses.field(default="get", init=False)
    danger: bool = dataclasses.field(default=False, init=False)
    # What its press posts, by name: one of the command's modes ("action":
    # "read"), a field its view reads ("status"), what it acts on when that
    # is any string, or the ``next`` it returns to. A press that sends more
    # than one is a form of its own, so it never stands inside another.
    sends: Mapping[str, str] = dataclasses.field(default_factory=dict, init=False, hash=False)
    # The press on the command's own confirmation, loud where it destroys.
    confirms: bool = dataclasses.field(default=False, init=False)

    @classmethod
    def command(
        cls,
        name: str,
        target: Target = "",
        *,
        variant: str = "",
        primary: bool = False,
        query: Query | None = None,
        refused: str = "",
    ) -> PageAction | None:
        """The control that runs the command ``name`` on ``target``, or None when the page's person may not run it.

        It opens the page the command's view draws, or posts to a view that
        draws none. Its words (``variant`` a declared mode), danger and address
        are the command's, and ``query`` is added to the address. It is drawn
        unusable, saying why, while the command would be refused for ``target``.
        """

        return cls._offered(name, target, variant, query, refused, primary=primary)

    @classmethod
    def post(
        cls,
        name: str,
        target: Target = "",
        *,
        sends: Mapping[str, str] | None = None,
        confirms: bool = False,
        variant: str = "",
        primary: bool = False,
        query: Query | None = None,
        refused: str = "",
    ) -> PageAction | None:
        """The press that posts ``sends`` to the command ``name`` on
        ``target``, built as ``command`` builds its control; ``confirms`` makes
        it the press on its view's confirmation."""

        return cls._offered(
            name,
            target,
            variant,
            query,
            refused,
            primary=primary,
            posts=True,
            sends=dict(sends or {}),
            confirms=confirms,
        )

    @classmethod
    def dialog(
        cls,
        name: str,
        modal: PageDialog | str,
        target: Target = "",
        *,
        variant: str = "",
        primary: bool = False,
        query: Query | None = None,
        refused: str = "",
    ) -> PageAction | None:
        """The control that opens the command ``name``'s form in ``modal`` on
        the page, built as ``command`` builds its control. Without a script it
        opens the page the command's view draws, or the command's own page
        where its view draws none."""

        return cls._offered(name, target, variant, query, refused, primary=primary, posts=False, modal=modal)

    @classmethod
    def _offered(
        cls,
        name: str,
        target: Target,
        variant: str,
        query: Query | None,
        refused: str,
        *,
        posts: bool | None = None,
        **built: Any,
    ) -> PageAction | None:
        if not runs_here(name):
            return None
        spec = command_spec(name)
        subject = "" if isinstance(target, Mapping) else str(target)
        url, answers_get = command_reach(name, target, variant)
        if posts is False and not answers_get:
            url = command_url(name, subject)
        if query:
            url = with_query(url, query)
        reason = refused or outbound_refusal(name, subject)
        return cls._built(
            label=spec.words(variant),
            url=url,
            title=reason,
            disabled=bool(reason),
            method="post" if (not answers_get if posts is None else posts) else "get",
            danger=spec.effect is Effect.DESTRUCTIVE,
            **built,
        )

    @classmethod
    def of(cls, link: Any) -> PageAction:
        """An ``ActionLink`` as a page control, drawn unusable, saying why,
        while its command would be refused."""

        if isinstance(link, PageAction):
            return link
        values = dataclasses.asdict(link) if dataclasses.is_dataclass(link) else dict(link)
        method = values.get("method", "GET").lower()
        refusal = values.get("refusal", "")
        return cls._built(
            label=values["label"],
            url=values["url"],
            primary=values.get("recommended", False),
            title=refusal or values.get("reason", ""),
            disabled=bool(refusal),
            method=method,
            danger=values.get("effect") == Effect.DESTRUCTIVE,
        )

    @classmethod
    def _built(cls, **values: Any) -> PageAction:
        """One with the fields only ``command`` and ``of`` set, past the link's rule."""

        if unknown := values.keys() - {each.name for each in dataclasses.fields(cls)}:
            raise TypeError(f"PageAction has no field {', '.join(sorted(unknown))}")
        action = object.__new__(cls)
        for each in dataclasses.fields(cls):
            if each.name in values:
                given = values[each.name]
            elif each.default_factory is not dataclasses.MISSING:
                given = each.default_factory()
            else:
                given = each.default
            object.__setattr__(action, each.name, given)
        return action

    def __post_init__(self) -> None:
        # A link to a command's page would say words of its own for it.
        if (name := runs_at(self.url)) is not None:
            raise ImproperlyConfigured(f"{self.url} runs {name!r}: build its control with PageAction.command.")

    @property
    def brought_dialog(self) -> PageDialog | None:
        """The dialog this action brings to the page, if it brings one."""

        return self.modal if isinstance(self.modal, PageDialog) else None

    @property
    def modal_id(self) -> str:
        """The id the link opens, wherever the dialog is drawn."""

        return self.modal.id if isinstance(self.modal, PageDialog) else self.modal

    @property
    def look(self) -> str:
        """How its button looks (``ui.BUTTON_LOOKS``)."""

        # Quiet: a head's destructive action is the rarest thing on the page
        # and should not be its loudest. Its confirmation is loud.
        danger = ("danger",) if self.confirms else ("ghost", "danger")
        return " ".join(
            (
                *(("primary",) if self.primary else ()),
                *(danger if self.danger else ()),
            )
        )

    @property
    def css(self) -> str:
        return button_classes(self.look)


def _reached_by(name: str, target: str, mode: str) -> str:
    """The address a row's control for the command ``name`` posts to, or ""
    when the person the page is for may not run it."""

    if not runs_here(name):
        return ""
    return command_reach(name, target, mode)[0]


@dataclass(frozen=True, slots=True)
class RowMark:
    """A mark a reader keeps on a row's record, such as a favorite's star, set
    or cleared by one press. ``row_actions`` draws it after the row's order,
    among its controls, so its column is one the table sizes."""

    url: str
    on: bool
    # The record's name and what the mark keeps it among, said by the press.
    name: str
    among: str

    @classmethod
    def command(cls, name: str, target: str, *, mode: str, on: bool, among: str) -> RowMark | None:
        """The mark the command ``name`` sets on ``target`` in its mode
        ``mode``, or None when the person the page is for may not set it."""

        url = _reached_by(name, target, mode)
        return cls(url, on, target, among) if url else None

    @property
    def title(self) -> str:
        return f"Remove {self.name} from {self.among}" if self.on else f"Add {self.name} to {self.among}"

    @property
    def press(self) -> PageAction:
        """The one press that sets or clears it, as the button draws it."""

        return _pressed("★" if self.on else "☆", self.url, title=self.title)


@dataclass(frozen=True, slots=True)
class RowOrder:
    """The pair that moves a row past its neighbor in an order the reader
    keeps (``direction`` up or down), each drawn only where the row has a
    neighbor that way."""

    url: str
    name: str
    up: bool
    down: bool

    @classmethod
    def command(cls, name: str, target: str, *, mode: str, up: bool, down: bool) -> RowOrder | None:
        """The pair the command ``name`` moves ``target`` by in its mode
        ``mode``, or None when the person the page is for may not move it."""

        url = _reached_by(name, target, mode)
        return cls(url, target, up, down) if url and (up or down) else None

    @property
    def presses(self) -> tuple[PageAction, ...]:
        """Its presses, up then down, each where the row has a neighbor that way."""

        ways = (("↑", "up", self.up), ("↓", "down", self.down))
        return tuple(
            _pressed(glyph, self.url, title=f"Move {self.name} {way}", sends={"direction": way})
            for glyph, way, there in ways
            if there
        )


def _pressed(label: str, url: str, *, title: str, sends: Mapping[str, str] | None = None) -> PageAction:
    """A row's own press on the address its command reached (``_reached_by``)."""

    return PageAction._built(label=label, url=url, title=title, method="post", sends=dict(sends or {}))


@dataclass(frozen=True, slots=True)
class PageBadge:
    """A short fact beside a page's title: its reach, that it is read-only."""

    label: str
    tone: Tone | None = None
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
    # A command's control its reader may not run is None, and left out here.
    actions: tuple[PageAction | Ask | None, ...] = ()
    navigation: PageNavigation | None = None
    # Where the page sits, as (label, url) pairs, when it is reached from a parent.
    trail: tuple[Crumb, ...] = field(default_factory=tuple)
    # What the page shows, by the names it is declared under
    # (``application.page_reads``).
    reads: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # The head offers only the links its reader may follow.
        actions = tuple(
            each
            for each in self.actions
            if each is not None and (not isinstance(each, PageAction) or offered_now(each.url))
        )
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

        found = (action.brought_dialog for action in self.actions if isinstance(action, PageAction))
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
    # The one capability the page reads under beside READ, where it needs one
    # (``page_capabilities``).
    required_capability: str

    def get_page_title(self) -> str:
        return self.page_title

    def get_page_lede(self) -> str:
        return self.page_lede

    def get_page_actions(self) -> tuple[PageAction | Ask, ...]:
        return ()

    def get_page_navigation(self) -> PageNavigation | None:
        return None

    def trail_label(self, record: Any) -> str | None:
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


def _names_its_read(cls: type) -> None:
    if not (getattr(cls, "resource", "") and getattr(cls, "context_object_name", "")):
        raise ImproperlyConfigured(f"{cls.__name__} names the resource it reads and its context_object_name.")


class RecordListMixin(PageMixin):
    """The list page of a resource kind, its rows the items that kind's API
    and MCP list read answer (``resources.every_item``), so a row says what an
    agent's list says. The template reads them as ``context_object_name``."""

    # The ``ResourceSpec`` whose list read draws the page, by its name.
    resource = ""
    context_object_name = ""
    # The fields of each item the page draws, as a field mask; none, its list's own.
    fields: tuple[str, ...] = ()
    records: list[dict[str, Any]]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        _names_its_read(cls)

    def list_query(self) -> dict[str, Any]:
        """The filters of the list's query the page asks with, from its own
        GET filters; none, the whole list."""

        return {}

    def get(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        principal = web_principal(request.user)
        self.records = every_item(
            self.resource, principal=principal, fields=list(self.fields) or None, query=self.list_query()
        )
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context[self.context_object_name] = self.records
        return context


class RecordPageMixin(PageMixin):
    """The page of one record of a resource kind, drawn from the record that
    kind's API and MCP read answer (``resources.get_resource``), so what the
    three say of a thing is computed once. The template reads it as
    ``context_object_name``; what else the view adds is presentation, or
    another kind's records about this one."""

    # The ``ResourceSpec`` whose read of one draws the page, by its name.
    resource = ""
    context_object_name = ""
    # The route's name for the identifier, where it is not the identifier's own (``pk``).
    url_kwarg = ""
    record: dict[str, Any]
    # The model whose stored row the records of other kinds about this one
    # are asked by, and the relations they read (``stored``).
    model: Any = None
    stored_related: tuple[str, ...] = ()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        _names_its_read(cls)

    @property
    def identifier(self) -> str:
        return str(resource_registry()[self.resource].identifier)

    def get(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        if (elsewhere := self.read(request, kwargs)) is not None:
            return elsewhere
        return super().get(request, *args, **kwargs)

    def read(self, request: Any, kwargs: dict[str, Any]) -> Any:
        """Read ``record``, the one the route names, or answer with where its
        page is when that is not here. A page that posts reads it first too."""

        # As the route wrote it, which the read parses as its kind's identifier.
        asked = str(kwargs[self.url_kwarg or self.identifier])
        try:
            self.record = get_resource(self.resource, asked, principal=web_principal(request.user), strict=False)
        except RecordNotFound as exc:
            raise Http404(str(exc)) from exc
        # Asked for by another of its names, or a record whose page is
        # another kind's, such as a machine's declaration or an event read
        # on its calendar: the page lives at the record's own address, never
        # at one off this site.
        url = str(self.record["url"])
        if unquote(urlsplit(url).path) == request.path:
            return None
        if not url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()}):
            raise ImproperlyConfigured(f"{self.resource!r} answered a page HQ does not serve: {url!r}.")
        return redirect(url)

    @cached_property
    def stored(self) -> Any:
        """The record's row as stored, with ``stored_related`` fetched."""

        identifier = self.identifier
        return self.model.objects.prefetch_related(*self.stored_related).get(**{identifier: self.record[identifier]})

    def get_page_title(self) -> str:
        return str(self.record[self.identifier])

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context[self.context_object_name] = self.record
        return context


def page_capabilities(view: Callable[..., Any]) -> tuple[str, ...]:
    """What the routed ``view`` reads under: READ, and the one ``required_capability`` it declares.

    READ is never replaced, since every page reads the estate around it. A
    declared but empty attribute is refused, since it meant to name one.
    """

    # A class view's attributes are its class's; a function view's are its own.
    owner: Any = vars(view).get("view_class", view)
    try:
        declared = owner.required_capability
    except AttributeError:
        return (str(Capability.READ),)
    if not declared:
        raise ImproperlyConfigured(f"{owner.__qualname__!r} must define required_capability.")
    return tuple(dict.fromkeys((str(Capability.READ), str(declared))))


# The pages the site header opens from every page: its search field, Needs
# you, and the user menu's connection.
HEADER_PAGES = frozenset({"search", "action_items", "connection"})


def unreached_pages() -> tuple[str, ...]:
    """Each page a reader could not find: one HQ frames, opened at an address
    that names no record, which neither the navigation nor the header lists
    and no ``page_trail`` places below another page.

    A page whose address names a record is reached from that record's link.
    A view learns its route only as the URL configuration mounts it, so this
    is asked of the composed routes, extensions' included, as HQ starts.
    """

    listed = {item.route for item in domain_navigation()} | HEADER_PAGES
    return tuple(
        found.name or found.path
        for found in routed()
        if isinstance(view := found.view, type)
        and issubclass(view, PageMixin)
        and "get" in view.http_method_names
        and hasattr(view, "get")
        and not any(part.pattern.regex.groups for part in (*found.mounts, found.pattern))
        and found.name not in listed
        and not view.page_trail
    )


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

    return principal.permits(*page_capabilities(view))


def page_refusal(user: Any, view: Callable[..., Any]) -> Refusal | None:
    """Why the signed-in ``user`` may not open the page ``view`` draws, recorded, or None when they may.

    Asked before the view runs, so a refused page has read nothing.
    """

    principal = web_principal(user)
    if principal_opens(principal, view):
        return None
    capability = next(each for each in page_capabilities(view) if not principal.permits(each))
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
        principal = self._reader()
        return principal is not None and principal_opens(principal, view)

    def runs(self, spec: Any) -> bool:
        """Whether they may run the command ``spec`` declares."""

        principal = self._reader()
        return principal is not None and spec.permitted_to(principal)

    def _reader(self) -> Any:
        if self.user is None:
            return None
        if self._principal is None:
            self._principal = web_principal(self.user)
        return self._principal


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


@dataclass(frozen=True, slots=True)
class CommandRoute:
    """The route a command's control reaches: the one whose view declares it ``runs`` the command."""

    name: str
    # Whether its view draws a page (a form, a confirmation) or only takes a post.
    answers_get: bool

    def address(self, target: str | int | Mapping[str, str]) -> str:
        """Its address for ``target``: the route's one argument, or its
        arguments by name; a route that takes none leaves the target to what
        the control sends."""

        if isinstance(target, Mapping):
            return str(reverse(self.name, kwargs=dict(target)))
        if target == "":
            return str(reverse(self.name))
        try:
            return str(reverse(self.name, args=[target]))
        except NoReverseMatch:
            return str(reverse(self.name))


@dataclass(frozen=True, slots=True)
class RouteNamed:
    """What a view ``runs`` when its route names the command: the one its URL
    argument ``argument`` names (the command's own page, an ask for declared
    work). No control is addressed to it by command, since it holds none."""

    argument: str = "name"


# What a view ``runs``: a command's name, or the name and one mode of it where
# a route of its own runs only that mode (linking a receipt to an expense
# beside editing it, one of its declared variants, ``capability_words``;
# pinning a service beside a domain); or the command its route names.
type Runs = tuple[str | tuple[str, str], ...] | RouteNamed


def declared_runs(view: Callable[..., Any]) -> tuple[tuple[str, str], ...]:
    """The commands the routed ``view`` declares it ``runs``, the one way a view
    says it, each as its name and variant ("" for the command as a whole).
    One whose route names the command (``RouteNamed``) holds none of its own.
    """

    declared = _declared(view)
    if isinstance(declared, RouteNamed):
        return ()
    return tuple((each, "") if isinstance(each, str) else each for each in declared)


def declares_runs(view: Callable[..., Any]) -> bool:
    """Whether the routed ``view`` says what it runs: its commands, or the one its route names."""

    return bool(_declared(view))


def _declared(view: Callable[..., Any]) -> Runs:
    """``runs`` as the routed ``view`` declares it. A class view's is read from
    it as its route builds it (``as_view``'s arguments), so one class can run a
    command per route; a function view's is its own attribute."""

    built = _built_view(view)
    return getattr(view if built is None else built, "runs", ())


def _built_view(view: Callable[..., Any]) -> Any:
    """A class view as its route builds it, or None for a function view, or
    for one that runs no command."""

    owner = vars(view).get("view_class")
    if owner is None or not hasattr(owner, "runs"):
        return None
    return owner(**getattr(view, "view_initkwargs", {}))


def _answers_get(view: Callable[..., Any]) -> bool:
    """Whether ``view`` draws a page by GET, by the methods it declares. A
    function view answers every method."""

    built = _built_view(view)
    return built is None or ("get" in built.http_method_names and hasattr(built, "get"))


# The route each command's control reaches, by the resolver that holds them:
# Django makes a new resolver whenever its URL caches are cleared.
_COMMAND_ROUTES: WeakKeyDictionary[Any, dict[tuple[str, str], CommandRoute]] = WeakKeyDictionary()


def command_route(name: str, variant: str = "") -> CommandRoute | None:
    """The route whose view runs the command ``name`` (in its mode
    ``variant``, else as a whole), or None where none does, which leaves it to
    the command's own page."""

    resolver = get_resolver()
    found = _COMMAND_ROUTES.get(resolver)
    if found is None:
        found = _COMMAND_ROUTES[resolver] = _commands_routed()
    return found.get((name, variant)) or found.get((name, ""))


def command_reach(name: str, target: str | int | Mapping[str, str] = "", variant: str = "") -> tuple[str, bool]:
    """Where a control for the command ``name`` on ``target`` goes, and
    whether it opens a page there by GET: the route whose view declares it
    ``runs`` the command (``command_route``), else the command's own page, ""
    where that is not mounted. Every control for a command, a page's and a
    projection's, is addressed here."""

    found = command_route(name, variant)
    if found is not None and (address := _reached(found, target)):
        return address, found.answers_get
    return command_url(name, "" if isinstance(target, Mapping) else str(target)), True


def _reached(route: CommandRoute, target: str | int | Mapping[str, str]) -> str:
    """``route``'s address for ``target``, or "" where its route names the
    record it runs on and none is given, which the command's own page asks for."""

    try:
        return route.address(target)
    except NoReverseMatch:
        return ""


def _commands_routed() -> dict[tuple[str, str], CommandRoute]:
    found: dict[tuple[str, str], CommandRoute] = {}
    for each in routed():
        if not each.name:
            continue
        route = CommandRoute(each.name, _answers_get(each.pattern.callback))
        for command in declared_runs(each.pattern.callback):
            kept = found.setdefault(command, route)
            if kept.name != route.name:
                raise ImproperlyConfigured(
                    f"{command!r} is run at {kept.name!r} and at {route.name!r}; a command's control reaches one route."
                )
    return found


def runs_at(url: str | Promise) -> str | None:
    """The command the page at ``url`` runs, or None for a page that runs
    none: the command's own page, or a route whose view declares it."""

    parts = urlsplit(str(url))
    if parts.scheme or parts.netloc:
        return None
    try:
        match = resolve(parts.path)
    except Resolver404:
        return None
    declared = _declared(match.func)
    if isinstance(declared, RouteNamed):
        return str(match.kwargs[declared.argument])
    runs = declared_runs(match.func)
    return runs[0][0] if runs else None


def offered_now(url: str | Promise) -> bool:
    """``offered`` to the person the request being answered is for.

    Outside a request there is nobody to ask, so nothing is withheld; the page
    gate still refuses whoever follows the link.
    """

    request = current_request()
    return request is None or offering_for(request)(url)


def runs_here(name: str) -> bool:
    """Whether the person the request being answered is for may run the
    command ``name``: a page draws its control, or the form that sends it,
    only then. Outside a request nothing is withheld; the command still
    refuses whoever runs it."""

    request = current_request()
    return request is None or offering_for(request).runs(command_spec(name))


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

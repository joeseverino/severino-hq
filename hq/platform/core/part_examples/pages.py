"""The examples of a page's own parts: its head, its asks, its frame and its dialogs."""

lazy from datetime import UTC, datetime, timedelta
lazy from uuid import UUID

lazy from django.contrib.auth import get_user_model
lazy from django.contrib.messages import ERROR, INFO, SUCCESS, WARNING
lazy from django.contrib.messages.storage.base import Message
lazy from django.test import RequestFactory

from hq.platform.core.part_examples import DRAWN_AT
from hq.platform.core.template_parts import Example
lazy from hq.domains.jobs.models import Job, JobState
lazy from hq.platform.application.appearance import THEME_CHOICES, Theme
lazy from hq.platform.application.asks import DONE, FAILED, QUEUED, RUNNING, WAITING, Ask, Standing
lazy from hq.platform.application.command_center import DiscoveryItem
lazy from hq.platform.application.freshness import CURRENT, DUE, STALE, Freshness, ReadCadence
lazy from hq.platform.application.pages import Page, PageAction, PageVisit
lazy from hq.platform.application.request_channel import TAILNET_CHANNEL
lazy from hq.platform.application.service_context import ServiceSection
lazy from hq.platform.application.tables import Cell, DataTable, Row, TableColumn
lazy from hq.platform.application.ui import PageNavigation, PageSection, button_classes
lazy from hq.platform.core.oidc import SIGNED_IN_SESSION_KEY
lazy from hq.platform.core.templatetags.action_tags import MOST_BESIDE


def _asked(label, ask):
    """A table named ``label`` of one connection, its read asked from its row."""

    rows = (Row((Cell("example-main"),), asks=(ask,)),)
    return {"table": DataTable(label, (TableColumn("Name", kind="identifier"),), rows)}


def _signed_in():
    """What the page frame reads of a signed-in example operator's request,
    as the context processors hand it to base.html: the sections, a group of
    them among them, the bar's button look, the theme and its choices, the connection's channel and
    the queue's count."""

    return {
        "SITE_NAME": "Example HQ",
        "user": get_user_model()(
            username="example-operator", first_name="Example", last_name="Operator", is_staff=True
        ),
        "nav_entries": (
            {"kind": "item", "label": "Dashboard", "url": "dashboard", "active": True},
            {
                "kind": "group",
                "label": "Example section",
                "active": False,
                "items": (
                    {"label": "Example search", "url": "search", "active": False},
                    {"label": "Example queue", "url": "action_items", "active": False},
                ),
            },
        ),
        "bar_button": button_classes("bar"),
        "THEME": Theme.SYSTEM,
        "THEME_CHOICES": THEME_CHOICES,
        "CONNECTION_CHANNEL": TAILNET_CHANNEL,
        "agents_paused": False,
        "profile_action_count": 3,
        "show_action_count": True,
    }


def _palette():
    """Command Center's results for a term, as the search page hands them to
    the palette: the estate first, a group of records found by their words,
    then the commands, with more matches than it shows."""

    def found(kind, label, summary, destination, *badges):
        return DiscoveryItem(kind, label, label, summary, "/example/", destination, badges)

    return {
        "q": "example",
        "palette_estate": [
            {
                "key": "estate",
                "label": "Machines, services and domains",
                "items": (
                    found("machine", "example-host-01", "An example machine", "Machine", "Online"),
                    found("service", "example.example.com", "An example service", "Service", "Public", "Proxied"),
                ),
            }
        ],
        "palette_search_groups": [
            {
                "scope": "projects",
                "label": "Projects",
                "items": (
                    {
                        "title": "Example project with a long descriptive name",
                        "badge": "example-01",
                        "url": "/example/",
                        "timestamp": datetime(2026, 3, 2, 9, 30, tzinfo=UTC),
                        "snippet": (("An ", False), ("example", True), (" line the search found.", False)),
                    },
                ),
            }
        ],
        "palette_groups": [
            {
                "key": "commands",
                "label": "Commands",
                "items": (found("command", "Example restart", "Restart an example service", "Command"),),
            }
        ],
        "palette_count": 4,
        "match_total": 9,
    }


# The page frame's header, as every signed-in page draws it: the sections,
# the doorway to search, the queue's count and the person's menu, whose
# popovers the survey opens. Its long name is cut beside the mark; a short
# one fits whole on a phone's bar.
SITE_HEADER = Example(context=_signed_in, address="/example/", frame="before")

SITE_HEADER_STATES = {
    "short-name": Example(
        '{% include "partials/_site_header.html" %}',
        context=lambda: {**_signed_in(), "user": get_user_model()(username="ex", is_staff=True)},
        address="/example/",
        frame="before",
    ),
    # A session signed in hours before the catalog's moment, which the
    # person's menu says in hours.
    "signed-in-hours": Example(
        '{% include "partials/_site_header.html" %}',
        context=lambda: {**_signed_in(), "request": _signed_in_hours_ago(5)},
        frame="before",
    ),
}


def _signed_in_hours_ago(hours):
    """The example operator's request at /example/, its session signed in ``hours`` before ``DRAWN_AT``."""

    request = RequestFactory().get("/example/")
    request.user = _signed_in()["user"]
    request.session = {SIGNED_IN_SESSION_KEY: (DRAWN_AT - timedelta(hours=hours)).isoformat()}
    return request


MODAL = Example(
    '<a href="/example/" data-modal-open="example">Open</a>'
    '{% include "partials/_modal.html" with id="example" title="Example dialog" '
    'body_template="partials/_empty_state.html" %}',
    context={"message": "No example yet."},
)


MODAL_STATES = {
    # Reference text behind an info button in a section's body (partials/_about.html).
    "about": Example(
        '{% section "Example endpoint" %}<p>An example line of prose in the frame.</p>'
        '{% include "partials/_about.html" with id="example-about" title="Example notes"'
        ' lede="An example lede, the line under the dialog\'s title." body_template="partials/_empty_state.html"'
        ' message="An example paragraph of reference text." %}{% endsection %}'
    ),
}


# A trail of steps a word of two letters long: each is a small target, so
# the survey's small targets hold it by its spacing from the next, the
# slash between them belonging to neither.
PAGE_HEAD = Example(
    context=lambda: {
        "page": Page(
            title="Example",
            trail=(("Ex", "/example/"), ("Ex", "/example/ex/"), ("Example", "/example/ex/example/")),
        )
    },
    heads_the_page=True,
)


PAGE_HEAD_STATES = {
    # A page that names more controls beside its title (``page_beside``): a
    # decision's controls side by side, folded into the menu where the head
    # is too narrow for them.
    "many-actions": Example(
        '{% include "partials/_page_head.html" %}',
        lambda: {
            "page": Page(
                title="Example playbooks",
                actions=(
                    Ask("Example read", "/example/refresh/", title="Reads the example repository again."),
                    PageAction("View diff", "/example/scan/"),
                    PageAction._built(label="Adopt all", url="/example/adopt/", method="post"),
                    PageAction._built(label="Reject all", url="/example/reject/", method="post"),
                ),
            ),
            "page_beside": MOST_BESIDE,
            "page_aside": "control_plane/_playbooks_read.html",
            "sources": {
                "catalog": {"branch": "main", "committer": "example-maintainer", "created_at": DRAWN_AT},
                "revision": "0123456789ab",
                "scan": True,
            },
        },
        heads_the_page=True,
    ),
}


# Among the page's messages, as a post's outcome is said: each a toast.
MESSAGES = Example(
    context=lambda: {
        "messages": [
            Message(level, f"An example {name} message.")
            for level, name in ((SUCCESS, "success"), (INFO, "info"), (WARNING, "warning"), (ERROR, "error"))
        ]
    },
)


# An error page, which titles itself outside the page frame: one with the
# search field, as a missing page offers it, and one naming the request
# id its log line carries, long enough to break at its dots.
ERROR_PAGE = Example(
    context={
        "code": "404",
        "title": "Page not found",
        "body": "The example link may be old, or what it pointed to was deleted.",
        "search": True,
    },
    heads_the_page=True,
)


ERROR_PAGE_STATES = {
    "request": Example(
        '{% include "partials/_error_page.html" with code="500" title="Something broke" '
        'body="The example error is in the log. Trying again is safe." request_id=request_id %}',
        {"request_id": "example-0123abcd.4567ef89.0123abcd4567"},
        heads_the_page=True,
    ),
    "linked": Example(
        '{% include "partials/_error_page.html" with code="500" title="Something broke" '
        'body="The example error is in the log. Trying again is safe." request_id=request_id error_url=error_url %}',
        {"request_id": "example-0123abcd.4567ef89.0123abcd4567", "error_url": "/errors/example-0123abcd/"},
        heads_the_page=True,
    ),
}


# A saved copy's About dialog body: what to try, its own pages as chips,
# and who made it.
DEMO_ABOUT = Example(context=lambda: {"saved_copy": DRAWN_AT})


# A saved copy's footer, which says from the page's corner what it is.
DEMO_FOOT = Example(frame="after")


# Command Center's palette as its dialog opens on it, waiting for its
# results; its results for a term, each kind of group with more than it
# shows; and a term nothing answers.
COMMAND_CENTER_STATES = {
    "results": Example('{% include "partials/_command_center.html#results" %}', _palette),
    "empty": Example('{% include "partials/_command_center.html#results" %}', {"q": "example-none"}),
}


ASK = Example(context=lambda: {"ask": ASK_STATES["states"].given()["idle"]})


ASK_STATES = {
    # A "do this now" control in a section's body: one never pressed, beside
    # one whose work ended and says how.
    "states": Example(
        '{% section "Example readings 2" %}<p>{% include "partials/_ask.html" with ask=idle %}</p>'
        '<p>{% include "partials/_ask.html" with ask=done %}</p>{% endsection %}',
        lambda: {
            "idle": Ask("Read example-host-01", "/example/read/", title="Reads example-host-01 again."),
            "done": Ask(
                "Read example-host-01",
                "/example/read/",
                standing=Standing(DONE, note="Read 12 example records.", since=DRAWN_AT - timedelta(minutes=3)),
            ),
        },
    ),
    # While its work is under way: busy, its elapsed time ticking, followed
    # at its status resource.
    "running": Example(
        '{% section "Example readings 3" %}{% include "partials/_ask.html" with ask=ask %}{% endsection %}',
        lambda: {
            "ask": Ask(
                "Read example-host-01",
                "/example/read/",
                standing=Standing(
                    RUNNING,
                    note="Reading every example record example-host-01 holds, which takes a minute.",
                    since=DRAWN_AT - timedelta(seconds=40),
                    percent=40,
                ),
                status_url="/example/read/status/",
                primary=True,
            )
        },
    ),
    # In a row's cell, where its word alone stands and its sentence opens
    # under the row: one that failed with a long sentence.
    "in-a-row": Example(
        '{% section "Example connections 1" %}{% data_table table %}{% endsection %}',
        lambda: _asked(
            "Example connections 1",
            Ask(
                "Example read",
                "/example/read/",
                standing=Standing(
                    FAILED,
                    note="The example provider refused the credential, so nothing was read this time.",
                    since=DRAWN_AT - timedelta(minutes=8),
                ),
                compact=True,
            ),
        ),
    ),
    # Waiting its turn, given a sentence that only says its word again: the
    # word alone in the cell, and no line under the row.
    "waiting": Example(
        '{% section "Example feeds 1" %}{% data_table table %}{% endsection %}',
        lambda: _asked(
            "Example feeds 1",
            Ask(
                "Example read",
                "/example/read/",
                standing=Standing(QUEUED, note=WAITING, since=DRAWN_AT - timedelta(seconds=20)),
                status_url="/example/read/status/",
                compact=True,
            ),
        ),
    ),
    # As a page's own action, beside a link, in its head.
    "in-the-head": Example(
        '{% include "partials/_page_head.html" %}',
        lambda: {
            "page": Page(
                title="Example host",
                actions=(
                    Ask("Example read", "/example/read/", title="Reads example-host-01 again."),
                    PageAction("Example edit", "/example/edit/"),
                ),
            )
        },
        heads_the_page=True,
    ),
    # Waiting in the head with a long sentence, which runs back from the
    # button over the head's free space.
    "in-the-head-waiting": Example(
        '{% include "partials/_page_head.html" %}',
        lambda: {
            "page": Page(
                title="Example playbooks",
                actions=(
                    Ask(
                        "Example read",
                        "/example/refresh/",
                        standing=Standing(
                            QUEUED,
                            note=(
                                "Nothing has taken it; it last took work 3 hours ago. On the example host, "
                                "example-host automation starts the worker or says what it lacks."
                            ),
                            since=DRAWN_AT - timedelta(minutes=4),
                        ),
                        status_url="/example/refresh/status/",
                    ),
                ),
            )
        },
        heads_the_page=True,
    ),
}


# A job under way, as far along as it knows; one waiting its turn, which
# knows nothing yet; one that failed with a long note.
JOB_PROGRESS = Example(
    context=lambda: {
        "job": Job(
            id=UUID(int=1),
            kind="example.import",
            label="Example import",
            state=JobState.RUNNING,
            percent=40,
            note="Read 40 of 100 example rows.",
            created_at=DRAWN_AT,
            heartbeat_at=DRAWN_AT,
        )
    },
)


JOB_PROGRESS_STATES = {
    "waiting": Example(
        '{% include "partials/_job_progress.html" %}',
        lambda: {"job": Job(id=UUID(int=2), kind="example.import", label="Example import", created_at=DRAWN_AT)},
    ),
    "failed": Example(
        '{% include "partials/_job_progress.html" %}',
        lambda: {
            "job": Job(
                id=UUID(int=3),
                kind="example.import",
                label="Example import of a file with a name long enough to wrap",
                state=JobState.FAILED,
                error="The example file has no header row, so none of its rows could be read as example records.",
                created_at=DRAWN_AT,
            )
        },
    ),
}


READING_AGE_STATES = {
    # A reading's age in each of its states: current, aged and due again (a
    # reading taken on open, no fault), out of date, the moment alone, none,
    # and none with why.
    "states": Example(
        '{% section "Example readings 4" %}{% facts %}'
        '{% fact "Example current" %}{% include "partials/_reading_age.html" with reading=current only %}{% endfact %}'
        '{% fact "Example due" %}{% include "partials/_reading_age.html" with reading=due only %}{% endfact %}'
        '{% fact "Example stale" %}{% include "partials/_reading_age.html" with reading=stale only %}{% endfact %}'
        '{% fact "Example moment" %}{% include "partials/_reading_age.html" with at=at only %}{% endfact %}'
        '{% fact "Example never" %}{% include "partials/_reading_age.html" only %}{% endfact %}'
        '{% fact "Example unread" %}{% include "partials/_reading_age.html" with unread=unread only %}{% endfact %}'
        "{% endfacts %}{% endsection %}",
        lambda: {
            "unread": "the registry refused an anonymous read (HTTP 401); the image may be private.",
            "current": Freshness(
                CURRENT, DRAWN_AT - timedelta(minutes=4), ReadCadence(timedelta(hours=1), timedelta(hours=1))
            ),
            "due": Freshness(
                DUE, DRAWN_AT - timedelta(minutes=22), ReadCadence(timedelta(minutes=2), timedelta(hours=1))
            ),
            "stale": Freshness(
                STALE, DRAWN_AT - timedelta(days=3), ReadCadence(timedelta(hours=1), timedelta(hours=1))
            ),
            "at": DRAWN_AT - timedelta(hours=2),
        },
    ),
}


VISIT_REFRESH_STATES = {
    # A page that asks for its readings when opened: the note that ends its
    # head's first line, and a region that fills itself holding its own.
    "states": Example(
        '{% include "partials/_page_head.html" %}{% include "partials/_visit_refresh.html" with visit=page_visit %}'
        '{% section "Example host 2" %}<p>An example line.</p>{% endsection %}',
        lambda: {
            "page": Page(title="Example host 2", trail=(("Example machines", "/example/"),)),
            "page_visit": PageVisit("machine", "example-host-01"),
        },
        heads_the_page=True,
    ),
    "region": Example(
        '{% card element="div" data_example_region=True %}{% include "partials/_visit_refresh.html" with '
        'subject="page" name="example" refresh="[data-example-region]" %}<p>An example line.</p>{% endcard %}'
    ),
}


PAGE_NAVIGATION = Example(context=lambda: {"navigation": PAGE_NAVIGATION_STATES["states"].given()["page"].navigation})


PAGE_NAVIGATION_STATES = {
    # A dense page's map, more links than a phone's row holds, over the
    # sections it opens.
    "states": Example(
        '{% include "partials/_page_head.html" %}{% include "partials/_page_navigation.html" with '
        "navigation=page.navigation %}{% for item in page.navigation.items %}"
        "{% section item.label id=item.id %}<p>An example line.</p>{% endsection %}{% endfor %}",
        lambda: {
            "page": Page(
                title="Example host",
                navigation=PageNavigation(
                    tuple(
                        PageSection(f"example-{number}", label)
                        for number, label in enumerate(
                            (
                                "Overview",
                                "Example readings 5",
                                "Example containers 3",
                                "Example certificates",
                                "Relationships",
                                "Example history",
                            )
                        )
                    )
                ),
            )
        },
        heads_the_page=True,
    ),
}


def _sections():
    """A detail page's bands: one wide table whose rows are the latest of
    more, a short one, and a folded one of readouts."""

    runs = ServiceSection(
        "example-runs",
        "Example runs",
        ("Run", "State", "Started", "Took", "Host", "Note"),
        tuple(
            (
                Cell(f"example-run-{at}", f"/example/runs/{at}/"),
                Cell("Done", tone="good"),
                Cell("2 hours ago"),
                Cell("40 s"),
                Cell("example-host-01"),
                Cell("An example note."),
            )
            for at in range(1, 4)
        ),
        more_label="All runs",
        more_url="/example/runs/",
    )
    ports = ServiceSection("example-ports", "Example ports", ("Port", "Open to"), ((Cell("443"), Cell("Everyone")),))
    readouts = ServiceSection(
        "example-config",
        "Example configuration",
        ("Name",),
        ((Cell("example.conf"),),),
        readouts=(("example.conf", "listen 443\nserver_name example.com"),),
        folded=True,
    )
    return {"sections": (runs, ports, readouts)}


# A detail page's bands ({% include "partials/_page_sections.html" %}), the
# wide ones drawn ahead of the short ones.
PAGE_SECTIONS = Example(
    '{% include "partials/_page_sections.html" with spans_row=True %}'
    '{% include "partials/_page_sections.html" with spans_row=False %}',
    _sections,
)

"""The examples of the parts that draw records: lists, readings, calendars and changes."""

lazy from collections.abc import Callable
lazy from dataclasses import asdict
lazy from datetime import UTC, date, datetime, timedelta

lazy from django.core.paginator import Paginator
lazy from django.templatetags.static import static

from hq.platform.core.part_examples import DRAWN_AT, NARROW, runs
from hq.platform.core.template_parts import Example
lazy from hq.platform.application.action_links import read_link
lazy from hq.platform.application.approvals import DECISIONS, _decision_link
lazy from hq.platform.application.calendar import CalendarEvent, CalendarSource, SourceView, calendar_month
lazy from hq.platform.application.card_items import Insight, ListRow, Timeline, TimelineItem
lazy from hq.platform.application.dashboard import finding_rows
lazy from hq.platform.application.drawings import AllocationBar, Share
lazy from hq.platform.application.entity_links import EntityLink
lazy from hq.platform.application.finding_model import CHECK_AGAIN, Finding, opens
lazy from hq.platform.application.finding_steps import Do, Run
lazy from hq.platform.application.github_estate import Repository
lazy from hq.platform.application.github_posture import posture_of
lazy from hq.platform.application.integration_specs import ListSort
lazy from hq.platform.application.pages import PageAction
lazy from hq.platform.application.relationships import Relationships
lazy from hq.platform.application.tone import Tone
lazy from hq.platform.application.topology_model import RelationGroup, Relationship


def _holding(entry: CalendarEvent) -> Callable[[date, date], tuple[CalendarEvent, ...]]:
    """A source's reader that holds ``entry`` alone."""

    return lambda _first, _last: (entry,)


def _month_gone_by(month=date(2026, 1, 1)):
    """``month`` seen from a day in it: each kind of entry on the days gone by,
    a bar running on past today, each in a series color whose fill sits
    closest to the ink a day gone by is set in. Each entry draws a month of
    its own, whose days no other's grid holds, so its days' ids are its own
    on the gallery's page."""

    today = month.replace(day=21)
    entries = {
        7: CalendarEvent(
            "example-bar", "Example window that runs past today", month.replace(day=12), month.replace(day=24)
        ),
        1: CalendarEvent("example-timed", "Example timed line", datetime(month.year, month.month, 14, 15, tzinfo=UTC)),
        6: CalendarEvent("example-all-day", "Example all-day line", month.replace(day=15)),
        8: CalendarEvent("example-mark", "Example mark", month.replace(day=13), mark=True),
    }
    sources = tuple(
        ("Example", CalendarSource(f"example.series-{slot}", f"Example {slot}", _holding(entry), slot=slot))
        for slot, entry in entries.items()
    )
    return calendar_month(month, today=today, sources=sources)


def _problems(*titles, family="", status="attention"):
    """The queue's items about a thing, each with its meaning and the check that reads again."""

    return list(
        finding_rows(
            "example",
            "Example",
            tuple(
                Finding(
                    rule="example",
                    subject=title,
                    severity=status,
                    title=title,
                    explanation="What it means for the thing this page is about, in one sentence.",
                    family=family,
                )
                for title in titles
            ),
        )
    )


def _repository_postures():
    """Repositories against their standard: a public one others can reach
    through a key that can write, a private one that meets it all, and one
    whose access was never read."""

    kept = {
        "collaborators": [{"login": "example", "role": "admin"}],
        "deploy_keys": [
            {"title": "example deploy", "read_only": True, "last_used": (DRAWN_AT - timedelta(days=2)).isoformat()}
        ],
        "token": "read",
        "pinning_required": True,
        "vulnerability_alerts": True,
    }
    reached = {
        **kept,
        "collaborators": [{"login": "example", "role": "admin"}, {"login": "example-two", "role": "write"}],
        "deploy_keys": [{"title": "example release", "read_only": False, "last_used": None}],
        "token": "write",
    }
    records = (
        ("example/example-site", {"private": False, "access": reached, "variables": ["EXAMPLE_HOST"]}),
        ("example/example-app", {"private": True, "access": kept, "variables": []}),
        ("example/example-notes", {"private": True}),
    )
    return [
        {"posture": posture_of(Repository(name, {"url": f"https://example.com/{name}", **record}, DRAWN_AT))}
        for name, record in records
    ]


def _paged(number):
    """A list's page ``number`` of three."""

    paginator = Paginator(range(60), 20)
    return {"is_paginated": True, "paginator": paginator, "page_obj": paginator.page(number)}


def _toolbar():
    """A list's table as its view hands it to the toolbar: words searched for,
    one filter with an option chosen, a toggle set and a sort."""

    return {
        "query": "example",
        "search_placeholder": "Search example hosts…",
        "filters": [
            {
                "name": "kind",
                "label": "Kind",
                "selected_count": 1,
                "options": [
                    {"value": "machine", "label": "Example machine", "selected": True},
                    {"value": "container", "label": "Example container", "selected": False},
                ],
            }
        ],
        "toggles": [{"name": "stale", "label": "Out of date only", "selected": True}],
        "sorts": (ListSort("name", "Name"), ListSort("-seen", "Last seen")),
        "selected_sort": "",
        "active_count": 3,
    }


# Each provider's last sweep, as ``inventory.inventory_state`` hands it over:
# one current, one out of date, one unreachable.
_INVENTORY = (
    {
        "kind": "example.records",
        "label": "Example records",
        "count": 12,
        "reachable": True,
        "error": "",
        "observed_at": DRAWN_AT - timedelta(minutes=5),
        "state": "readable",
        "state_label": "Readable",
        "refused_parts": (),
    },
    {
        "kind": "example.hosts",
        "label": "Example hosts",
        "count": 4,
        "reachable": True,
        "error": "",
        "observed_at": DRAWN_AT - timedelta(days=2),
        "state": "stale",
        "state_label": "Out of date",
        "refused_parts": ("Example addresses not read",),
    },
    {
        "kind": "example.zones",
        "label": "An example provider with a long name",
        "count": 0,
        "reachable": False,
        "error": "The example provider refused the credential.",
        "observed_at": None,
        "state": "refused",
        "state_label": "Refused",
        "refused_parts": (),
    },
)


# The open problems about the thing a page shows: an urgent one in its
# box above, and the rest in a card of their own.
PROBLEMS = Example(
    context=lambda: {
        "problems": [
            *_problems("example-host-01 is out of disk", status="serious"),
            *_problems("example-web-01 runs an image by tag alone", "example-worker-02 restarts often"),
        ]
    },
)


PROBLEMS_STATES = {
    # Several of one family: one row saying what kind and how many, opening to them.
    "family": Example(
        '{% section "Example containers 2" %}{% include "partials/_problems.html#rest" with rest=rest %}{% endsection %}',
        lambda: {
            "rest": _problems(
                "example-web-07 runs an image by tag alone",
                "example-web-08 runs an image by tag alone",
                "example-web-09 runs an image by tag alone",
                family="Example images by tag",
            )
        },
    ),
}


# A list whose rows carry their own padding fills the frame as a table does.
RECORD_LIST = Example(
    context=lambda: {
        "rows": [
            ListRow("example-one", url="/example/one/", detail="Example access, read only"),
            ListRow("example-two", url="/example/two/", detail="Example access, read only"),
        ]
    },
)


RECORD_LIST_STATES = {
    # A list of records, each row's actions ending its line: one that cannot
    # be pressed, glyphs named by their titles, and a word; a row with a
    # state, a line of meta and a day, and one leaving HQ.
    "actions": Example(
        '{% section "Example groups" %}{% include "partials/_record_list.html" %}{% endsection %}',
        lambda: {
            "rows": [
                ListRow(
                    "Example savings",
                    detail="3 example accounts",
                    actions=(
                        PageAction._built(
                            label="↑", url="/example/group/", method="post", value="up", title="Move up", disabled=True
                        ),
                        PageAction._built(
                            label="↓", url="/example/group/", method="post", value="down", title="Move down"
                        ),
                        PageAction._built(
                            label="Remove", url="/example/group/", method="post", value="delete", danger=True
                        ),
                    ),
                ),
                ListRow(
                    "Example checking",
                    url="/example/checking/",
                    status=Tone.ATTENTION,
                    badge="Needs review",
                    meta="2 example accounts",
                    at=date(2026, 11, 2),
                ),
                ListRow("Example docs", url="https://example.com/", external=True),
            ]
        },
    ),
    # Rows named by the things they link, as a record's related lists and
    # the dashboard's projects draw them: a note, a day, a long name, and a
    # project's category as its color.
    "links": Example(
        '{% section "Example related" %}{% include "partials/_record_list.html" %}{% endsection %}',
        lambda: {
            "rows": [
                ListRow(
                    link=EntityLink("example-host-one", "/example/host/", kind="machine", kind_label="machine"),
                    meta="An example note",
                ),
                ListRow(
                    link=EntityLink(
                        "An example document with a title long enough to wrap",
                        "/example/document/",
                        kind="document",
                        kind_label="document",
                    ),
                    detail="Example category",
                    at=date(2026, 11, 2),
                ),
                ListRow(
                    link=EntityLink("Example project", "/example/project/", kind="project", kind_label="project"),
                    detail="Homelab",
                    category="homelab",
                    at=date(2026, 11, 3),
                ),
            ]
        },
    ),
    # Places to go, as search draws what it found, each row its link: a
    # place with its chips, one with a line long enough to wrap and more
    # chips than fit beside it, and one with no page of its own.
    "places": Example(
        '{% section "Example places" count=3 %}'
        '{% include "partials/_record_list.html" with whole_rows=True %}{% endsection %}',
        lambda: {
            "rows": [
                ListRow("Example host one", url="/example/host/", detail="Example machine", chips=("Running",)),
                ListRow(
                    "Example connection",
                    url="/example/connection/",
                    detail="Reads the example store's accounts, balances and the transactions under them",
                    chips=("Reads accounts", "Reads balances", "Reads transactions"),
                ),
                ListRow("Example check", detail="An example check with no page"),
            ]
        },
    ),
    # Search hits, each with where the record matched under its line: a
    # match inside a word, a snippet long enough to be cut at its second
    # line, and record text that is not HQ's words.
    "snippets": Example(
        '{% section "Example results" count=3 %}{% include "partials/_record_list.html" %}{% endsection %}',
        lambda: {
            "rows": [
                ListRow(
                    "Example backup host",
                    url="/example/host/",
                    detail="Machine",
                    at=date(2026, 11, 2),
                    snippet=(("Runs the nightly ", False), ("backup", True), ("s for example-host-one.", False)),
                ),
                ListRow(
                    "An example note with a long title that wraps on a phone",
                    url="/example/note/",
                    detail="Note",
                    snippet=(
                        ("The example ", False),
                        ("backup", True),
                        (
                            (
                                " runs at 02:00 and copies every example volume to the example store, then checks"
                                " each copy against its source and writes what it found to the example log, which"
                                " the morning report reads before anyone is awake to read it."
                            ),
                            False,
                        ),
                    ),
                ),
                ListRow(
                    "Example audit entry",
                    url="/example/audit/",
                    snippet=(("enabled: False to True at 2026-11-02 09:30, ", False), ("backup", True)),
                ),
            ]
        },
    ),
}


ALLOCATION_STATES = {
    # The bar with its legend, as a card of a whole and its parts draws it: each part's
    # swatch, amount (debt in its tone), label and share, each a link onward.
    "legend": Example(
        '{% section "Example net worth" figure=True %}'
        '{% include "partials/_allocation.html" with legend=True stacked=True %}'
        "{% endsection %}",
        lambda: {
            "allocation": AllocationBar(
                "Example assets by kind",
                (
                    Share("Cash", 4200, 1, url="/example/?type=cash", detail="$4,200.00"),
                    Share("Investments", 12800, 3, url="/example/?type=investment", detail="$12,800.00"),
                    Share("Owed", 1500, 2, url="/example/?type=owed", detail="-$1,500.00", tone=Tone.ATTENTION),
                ),
            )
        },
    ),
}


ALLOCATION = Example(context=ALLOCATION_STATES["legend"].given)


EMPTY_STATE = Example(context={"message": "No example hosts yet."})


EMPTY_STATE_STATES = {
    # An absence said where a value would stand, beside a value that is
    # there, in a list of facts and in a table's cell.
    "inline": Example(
        '{% section "Example runs on" %}{% facts %}'
        '{% fact "Example container" %}{% include "partials/_empty_state.html" with message="No example container is known." inline=True %}{% endfact %}'
        '{% fact "Example image" value="example-1.2.3" %}'
        "{% endfacts %}{% endsection %}"
    ),
}


# A calendar's sources, each a toggle in its color: one shown with what it
# shows, one hidden, one that could not be read; and a source's swatch.
CALENDAR_SOURCES = Example(
    context=lambda: {
        "sources": (
            SourceView(
                "example.shown",
                "Example shown",
                group="",
                slot=1,
                shown=True,
                description="Every example event, as it is planned.",
            ),
            SourceView(
                "example.hidden",
                "Example hidden",
                group="",
                slot=4,
                shown=False,
                description="Each example that ran.",
            ),
            SourceView(
                "example.failed",
                "Example unread",
                group="",
                slot=7,
                shown=True,
                failure="Example source did not answer in 5 s.",
            ),
        )
    },
)


# A month with days gone by, each kind of entry on them: their words are
# still links a reader follows, so they read as text in play does.
MONTH_GRID = Example(
    '{% section "Example month" %}{% include "partials/_month_grid.html" %}{% endsection %}',
    context=lambda: {"calendar": _month_gone_by()},
)


MONTH_GRID_STATES = {
    # A month compact, as the dashboard draws it beside its recent projects:
    # where things are, each a dot or a sliver, the day opening the rest.
    "compact": Example(
        '{% layout %}{% section "Example month 2" %}'
        '{% include "partials/_month_grid.html" with compact=True %}{% endsection %}'
        '{% section "Example projects" %}{% include "partials/_record_list.html" %}{% endsection %}{% endlayout %}',
        lambda: {
            "calendar": _month_gone_by(date(2026, 5, 1)),
            "rows": [
                ListRow(
                    f"Example project {number}",
                    url=f"/example/project-{number}/",
                    detail="Software",
                    at=date(2026, 1, 20 - number),
                )
                for number in range(1, 7)
            ],
        },
    ),
    # A month as the calendar page draws it, the main of a `.split` beside a
    # flow of sections: the month takes the room, the sections keep their
    # basis beside it and pair up once they wrap under it.
    "split": Example(
        '{% layout kind="split" %}{% section "Example month 3" %}{% include "partials/_month_grid.html" %}'
        '{% endsection %}{% layout aside=True %}{% section "Example identity 6" %}' + NARROW + "{% endsection %}"
        '{% section "Example link 6" %}' + NARROW + "{% endsection %}{% endlayout %}{% endlayout %}",
        lambda: {"calendar": _month_gone_by(date(2026, 9, 1))},
    ),
}


# A note in its own words with the names HQ knows in it as links, its
# line breaks kept, and a name with no break in it.
LINKED_TEXT = Example(
    context=lambda: {
        "parts": (
            "Deploys to ",
            EntityLink("example-host-one", "/example/host/", kind="machine", kind_label="machine"),
            " behind ",
            EntityLink("example-proxy", "/example/proxy/"),
            ".\nRestart it there after a change to example-configuration-with-a-long-unbroken-name.yaml.",
        )
    },
)


# Readings that need reading, each tone: one a link, one with its next
# step, one with neither a value nor an eyebrow.
INSIGHT_GRID = Example(
    '{% section "Example insights" %}'
    '{% include "partials/_insight_grid.html" with label="Example insights" %}{% endsection %}',
    context=lambda: {
        "items": [
            Insight(
                Tone.ATTENTION,
                "Example readiness",
                "Example records missing a category",
                "12",
                "Uncategorized example records are left out of every example total.",
                action="Give each one a category.",
                url="/example/records/",
            ),
            Insight(Tone.GOOD, "Example pace", "Example pace this month", "On track", "Ahead of the example plan."),
            Insight(
                Tone.SERIOUS,
                "Example spend",
                "An example reading with a title long enough to wrap on a phone",
                "$1,204.50",
                "Spent past the example budget by a margin worth reading about.",
            ),
            Insight(Tone.NEUTRAL, "", "An example reading with nothing to show", "", "An example interpretation."),
        ]
    },
)


INSIGHT_GRID_STATES = {
    # Insights standing under the page's own title, as a domain's mail stages
    # do, their titles a level under it; one wraps a long reading at its
    # seams, one carries its next step.
    "under-title": Example(
        '{% include "partials/_insight_grid.html" with label="Example stages" level=2 %}',
        lambda: {
            "items": [
                Insight(
                    Tone.NEUTRAL,
                    "Example receiving",
                    "Who accepts example mail?",
                    "mx1.example.com, mx2.example.com, mx3.example.com",
                    "Three example servers, tried in priority order.",
                ),
                Insight(
                    Tone.ATTENTION,
                    "Example enforcing",
                    "What happens to failing example mail?",
                    "Delivered anyway",
                    "Example failures are reported and delivered.",
                    action="Published but not enforcing: failures are delivered.",
                ),
            ]
        },
    ),
}


DOCUMENT_PICTURE = Example(context=lambda: {"src": static("img/logo.png"), "alt": "An example receipt"})


DOCUMENT_PICTURE_STATES = {
    # A picture of a document in a figure's section, square where its box is a
    # page's shape, so it sits at the top of the room held for it.
    "states": Example(
        '{% load static %}{% static "img/logo.png" as picture %}'
        '{% section "Example file" figure=True lede="example-receipt.png" %}'
        '{% include "partials/_document_picture.html" with src=picture alt="An example receipt" only %}'
        "{% endsection %}",
    ),
}


DOTS = Example(context=lambda: {"dots": runs()})


DOTS_STATES = {
    # A history in a table's cell.
    "in-a-row": Example(
        '{% section "Example work" %}{% table_region "Example work" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Name" "identifier" %}{% table_heading "Runs" %}</tr></thead><tbody>'
        '<tr><td>example.read</td><td>{% include "partials/_dots.html" with dots=runs only %}</td></tr>'
        "</tbody></table>{% endtable_region %}{% endsection %}",
        lambda: {"runs": runs()},
    ),
}


PAGINATION_STATES = {
    # A list's pages, at a middle page, and on the first, which has none before it.
    "states": Example(
        '{% section "Example events" %}{% table_region "Example events" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Event" %}</tr></thead><tbody><tr><td>Example event</td></tr></tbody></table>'
        '{% endtable_region %}{% include "partials/_pagination.html" %}{% endsection %}',
        lambda: _paged(2),
        address="/example/events/?q=example&page=2",
    ),
    "first": Example(
        '{% section "Example events 2" %}{% include "partials/_pagination.html" %}{% endsection %}',
        lambda: _paged(1),
        address="/example/events/",
    ),
}


PAGINATION = Example(context=PAGINATION_STATES["states"].given, address="/example/")


TABLE_TOOLBAR_STATES = {
    # A list's toolbar over its table: a search with words in it, a filter
    # with some of its options chosen, a toggle and a sort.
    "states": Example(
        '{% include "partials/_table_toolbar.html" %}{% table_region "Example hosts" %}<table class="data-table">'
        '<thead><tr>{% table_heading "Name" "identifier" %}{% table_heading "State" "status" %}</tr></thead>'
        "<tbody><tr><td>example-01</td><td>Online</td></tr><tr><td>example-02</td><td>Offline</td></tr>"
        "</tbody></table>{% endtable_region %}",
        lambda: {"table": _toolbar()},
        address="/example/hosts/?q=example&kind=machine&stale=1",
    ),
}


# How old each provider's last reading is: folded as a grid, one of them
# out of date and one unreachable; one provider's as a sentence; and its
# age alone, in a heading's aside.
SWEEP_FRESHNESS = Example(context=lambda: {"inventory": _INVENTORY})


SWEEP_FRESHNESS_STATES = {
    "one": Example(
        '{% section "Example records 2" %}{% include "partials/_sweep_freshness.html" with only_kind="example.records" %}'
        "{% endsection %}",
        lambda: {"inventory": _INVENTORY},
    ),
    "inline": Example(
        '{% section "Example records 3" %}<p>An example line.</p>{% aside %}<span class="count">12 example records'
        '{% include "partials/_sweep_freshness.html" with only_kind="example.records" inline=True %}</span>'
        "{% endsection %}",
        lambda: {"inventory": _INVENTORY},
    ),
}


# A horizon of dated events, more than one column holds on a wide
# screen: a link, one with a line under it and a badge, each tone.
TIMELINE = Example(
    context=lambda: {
        "timeline": Timeline(
            "Example horizon",
            "What is coming up for the example records.",
            (
                TimelineItem(date(2026, 1, 5), "Example renewal", url="/example/renewal/"),
                TimelineItem(
                    date(2026, 1, 19),
                    "Example certificate with a name long enough to wrap",
                    detail="example-host-01.example.com",
                    status=Tone.ATTENTION,
                    badge="Due soon",
                ),
                TimelineItem(date(2026, 2, 2), "Example review", detail="Every quarter", status=Tone.GOOD),
                TimelineItem(date(2026, 3, 16), "Example payment", status=Tone.SERIOUS, badge="Overdue"),
            ),
        )
    },
)


# What a record is related to: a group whose ends each say who read them
# and when, one a single source read, and one of names alone.
RELATIONSHIPS = Example(
    context=lambda: {
        "relationships": Relationships(
            "example-host-01",
            groups=(
                RelationGroup(
                    "Reached through",
                    1,
                    (
                        Relationship(
                            EntityLink("example-proxy-01", "/example/proxy/", kind="machine"),
                            source=EntityLink("Example provider", "/example/provider/"),
                            observed_at=DRAWN_AT - timedelta(hours=3),
                        ),
                        Relationship(
                            EntityLink("example-proxy-02", "/example/proxy-2/", detail="an example record"),
                            source=EntityLink("Another example provider", "/example/provider-2/"),
                            observed_at=DRAWN_AT - timedelta(days=2),
                            stale=True,
                        ),
                    ),
                ),
                RelationGroup(
                    "Runs",
                    2,
                    tuple(
                        Relationship(
                            EntityLink(f"example-service-{number}", f"/example/service-{number}/"),
                            source=EntityLink("Example provider", "/example/provider/"),
                            observed_at=DRAWN_AT - timedelta(minutes=20),
                        )
                        for number in range(1, 6)
                    ),
                ),
                RelationGroup(
                    "Names",
                    3,
                    (Relationship(EntityLink("example.com")), Relationship(EntityLink("www.example.com"))),
                ),
            ),
            focus_url="/example/topology/",
        )
    },
)


# Repositories against their standard, each a row of its name, its meter
# and its score: one failing that others can reach, one meeting it all,
# and one whose access was never read.
REPOSITORY_POSTURES = Example(
    context=lambda: {"repositories": _repository_postures()},
)


# A share of the largest, then one too small to round up, which still shows.
METER = Example(
    '{% include "partials/_meter.html" with value=64 max=100 only %}'
    '{% include "partials/_meter.html" with value=0.4 max=100 only %}'
)
METER_STATES = {
    # Checks of two groups, each met, unmet or not read, as a repository's are.
    "marks": Example(
        '{% include "partials/_meter.html" with marks=marks label="4 of 5 checks pass; example is off" only %}',
        {
            "marks": (
                {"tone": Tone.GOOD, "group": "every"},
                {"tone": Tone.GOOD, "group": "every"},
                {"tone": Tone.SERIOUS, "group": "every"},
                {"tone": Tone.GOOD, "group": "public"},
                {"tone": Tone.NEUTRAL, "group": "public"},
            )
        },
    ),
}


CHANGE_STATES = {
    # A value and the one that replaces it: as words, and as a cell's own
    # identifiers, one pair linking outside HQ and one too long to share a
    # line with its old value beside a note that asks for its whole length.
    "states": Example(
        '{% section "Example versions" %}<p>{% include "partials/_change.html" with from="1.2" to="1.4" %}</p>'
        '{% table_region "Example images" %}<table class="data-table"><thead><tr>'
        '{% table_heading "Name" "identifier" %}{% table_heading "Image" %}{% table_heading "Note" %}</tr></thead>'
        '<tbody><tr><td>example-01</td><td>{% include "partials/_change.html" with from="example:1.2" '
        'to="example:1.4" url="https://example.com/releases/" code=True %}</td><td>An example release</td></tr>'
        '<tr><td>example-02</td><td>{% include "partials/_change.html" with from=old to=new code=True %}'
        "</td><td>Restarted once the example update was pulled from the example registry</td></tr></tbody>"
        "</table>{% endtable_region %}{% endsection %}",
        {"old": "example:2026.1.0", "new": "example:2026.3.0"},
    ),
}


ICON = Example(context={"name": "check-circle"})


ROW_FOLD = Example(context={"label": "Show all of example-host-01"})


WORK_QUEUE = Example(
    context=lambda: {"items": _problems("Example certificate expires soon", "Example backup is late"), "grouped": True}
)


def _opened_problems():
    """Rows that open onto what was read and what to do: commands whose
    labels differ in length, and one step alone between the facts and the
    reason it cannot be done from here."""

    facts = (("Waiting since", "Jan 3, 1:42 AM"), ("Packages", "example-kernel, example-libc"))
    return list(
        finding_rows(
            "example",
            "Example",
            (
                Finding(
                    rule="example",
                    subject="example-host-01",
                    severity=Tone.ATTENTION,
                    title="example-host-01 waits on a restart",
                    explanation="An installed update takes effect only once the machine restarts.",
                    evidence=facts,
                    steps=(
                        Run("See what waits on the restart", 'ssh example-host-01 "cat /run/reboot-required.pkgs"'),
                        Run("See when", 'ssh example-host-01 "shutdown --show"', notes=("shutdown -c cancels it.",)),
                    ),
                ),
                Finding(
                    rule="example",
                    subject="example.com",
                    severity=Tone.SERIOUS,
                    title="Example certificate expired",
                    explanation="It serves no name, so nothing breaks when it expires.",
                    evidence=(("Certificate", "example.com"), ("Held in", "example-proxy")),
                    steps=(Do("Renew the certificate where it is held, then press Check again."),),
                    no_help_reason="HQ cannot renew this certificate.",
                ),
            ),
        )
    )


def _glance():
    """The dashboard's short queue: a held request whose meaning outruns its line, then two failing runs."""

    held = "00000000-0000-4000-8000-000000000001"
    said = "Asked with: machines set to example-host-01, security only set to yes, and 2 more fields."
    asked = Finding("approval", held, "example-agent wants to run a playbook", Tone.ATTENTION, said, held=True)
    run, check = opens("Open run", "https://github.example/runs/1"), read_link("verify", CHECK_AGAIN, "/findings/")
    failing = [
        Finding("example", n, f"{n} is failing", Tone.SERIOUS, "Failing job: test", primary=run, check=check)
        for n in ("example-app", "example-site")
    ]
    first, *runs = finding_rows("example", "Example", (asked, *failing))
    items = [{**first, "actions": [asdict(_decision_link(held, decision)) for decision in DECISIONS]}, *runs]
    return {"items": items, "grouped": True, "triage": True, "glance": True}


WORK_QUEUE_STATES = {
    # The dashboard's form, each row's meaning on its title's line.
    "glance": Example('{% include "partials/_work_queue.html" %}', _glance),
    # Rows opened as the queue page draws them: what each opens onto starts
    # where its title does, commands in one column, a lone step close.
    "opened": Example(
        '{% include "partials/_work_queue.html" %}', lambda: {"items": _opened_problems(), "grouped": True}
    ),
}

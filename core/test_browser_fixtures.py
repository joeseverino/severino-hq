"""Synthetic dashboard content for browser layout checks; no database reads."""

from datetime import date, datetime, timedelta, timezone

from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import RequestFactory

from application.ui import (
    ActivityCalendar,
    CalendarDay,
    ChartSeries,
    DomainOverview,
    Kpi,
    stacked_bar_chart,
)


def dashboard_html(*, populated=True, contacts=False):
    series = (ChartSeries("Example series", (2, 5, 3, 6), 1),)
    chart = stacked_bar_chart(
        "Example movement",
        "A short description.",
        ("One", "Two", "Three", "Four"),
        series,
        unit="units",
    )
    calendar = ActivityCalendar(
        "Example calendar",
        "A deliberately taller six-week calendar.",
        tuple(
            tuple(
                CalendarDay(
                    date(2026, 1, 5) + timedelta(days=week * 7 + day),
                    "done",
                    (1,),
                    "Example activity",
                )
                for day in range(7)
            )
            for week in range(6)
        ),
        series=series,
        period_label="Example period",
    )
    short = tuple(Kpi(f"Reading {i}", i, url="/example/") for i in range(4))
    long = tuple(
        Kpi(
            f"Longer example reading {i}",
            i,
            "An intentionally longer reporting window and explanation.",
            "/example/",
        )
        for i in range(4)
    )
    highlights = [
        {
            "id": "example.first",
            "label": "Example one",
            "overview": DomainOverview("Example", "/example/", short),
        },
        {
            "id": "example.second",
            "label": "Example two",
            "overview": DomainOverview(
                "Example", "/example/", long, (chart,), (calendar,)
            ),
        },
    ]
    request = RequestFactory().get("/")
    user = get_user_model()(username="example")
    request.user = user
    return render_to_string(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "SITE_NAME": "Example HQ",
            "dashboard_highlights": highlights if populated else [],
            "dashboard_cards": [
                {"label": "Example count", "value": 3, "url": "/example/"}
            ],
            "dashboard_panels": [
                {
                    "id": "example.machine",
                    "label": "Example machine",
                    "stale": True,
                    "observed_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
                    "payload": {"status": "good", "summary": "Example observation"},
                    "readings": [
                        {
                            "label": "CPU",
                            "display_label": "CPU",
                            "value": "12%",
                            "percent": 12,
                        }
                    ],
                }
            ],
            "active_projects": [{"name": "Example project", "slug": "example-project"}],
            "active_project_count": 1,
            "published_rows": [{"title": "Example publication", "url": "/example/"}],
            "recent_contacts": [{"title": "Example contact"}] if contacts else [],
            "external_links": [
                {
                    "href": "/example/",
                    "label": f"Example link {i}",
                    "sub": "Example endpoint",
                }
                for i in range(24)
            ],
            "recent_audit": [
                {
                    "url": "/example/",
                    "object_repr": "Example project",
                    "action_label": "Updated",
                    "actor": "example",
                    "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
                }
            ],
        },
    )

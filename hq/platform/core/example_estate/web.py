"""The public site at the zone's apex: its pages, a quarter of the visits
Cloudflare counted, and what people sent through its contact form."""

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from django.utils import timezone

from hq.domains.analytics.contracts import BACKFILL_DAYS, completed_window
from hq.domains.control_plane.observations.cloudflare import CONTACT_SUBMISSION_KIND
from hq.platform.application.analytics import record_analytics
from hq.platform.application.inventory_testing import store
from hq.platform.application.security import cli_principal
lazy from hq.domains.content.models import ContentItem
lazy from hq.domains.docs_index.models import DocumentationRecord

from .names import CLOUDFLARE, ZONE, hexed, iso
from .records import WRITEUPS

SITE = f"https://{ZONE}"
# The runbook the site's pages cite.
SITE_RUNBOOK = "rb-public-site"
# Slug, title, path, how long ago it was published: the site's standing pages.
PAGES = (
    ("about", "About", "/about/", 400),
    ("contact", "Contact", "/contact/", 400),
)
# The account and database the contact form writes to.
ACCOUNT = hexed("cloudflare", "account", length=32)
DATABASE = "contact-form"
DATABASE_ID = str(UUID(hexed("d1", DATABASE, length=32)))

# A day's page views at the home page; every other path is a share of it.
HOME_VIEWS = 60
# Each writeup's draw beside the home page's, newest and most linked first.
DRAW = {"/": 1.0, "/writeups/": 0.35, "/about/": 0.25, "/contact/": 0.08}
# How the visits split, by each breakdown Cloudflare reports.
SPLITS = {
    "referrer": (
        ("search.example.net", 0.38),
        ("github.com", 0.22),
        ("news.example.org", 0.14),
        ("lobsters.example.com", 0.06),
    ),
    "country": (
        ("US", 0.52),
        ("DE", 0.11),
        ("GB", 0.1),
        ("CA", 0.09),
        ("NL", 0.06),
        ("FR", 0.05),
        ("IN", 0.04),
        ("AU", 0.03),
    ),
    "device": (("desktop", 0.64), ("mobile", 0.33), ("tablet", 0.03)),
    "browser": (("Chrome", 0.48), ("Safari", 0.27), ("Firefox", 0.17), ("Edge", 0.08)),
    "os": (("macOS", 0.31), ("Windows", 0.24), ("iOS", 0.21), ("Android", 0.12), ("Linux", 0.12)),
}


def pages():
    today = timezone.localdate()
    for slug, title, path, days in PAGES:
        item = ContentItem.objects.create(
            title=title,
            slug=slug,
            content_type=ContentItem.Type.PAGE,
            status=ContentItem.Status.PUBLISHED,
            published_url=f"{SITE}{path}",
            published_at=today - timedelta(days=days),
        )
        # How the site is published and its messages answered.
        item.related_documentation.set(DocumentationRecord.objects.filter(doc_id=SITE_RUNBOOK))


def _draws(day_age):
    """Each path's share of the home page's views on a day ``day_age`` ago:
    a writeup only once it is published, read most in its first weeks."""

    draws = dict(DRAW)
    for slug, _title, _project, published in WRITEUPS:
        if day_age > published:
            continue
        fresh = max(0.0, 1 - (published - day_age) / 30)
        draws[f"/writeups/{slug}/"] = 0.12 + 0.9 * fresh
    return draws


def _views(path, day, base):
    """A day's views of one path: its draw, quieter at weekends, never quite the same twice."""

    weekend = 0.7 if day.weekday() >= 5 else 1.0
    noise = 0.8 + int(hexed("views", path, day), 16) % 41 / 100
    return round(HOME_VIEWS * base * weekend * noise)


def _day(day, day_age):
    rows = []
    for path, base in _draws(day_age).items():
        if views := _views(path, day, base):
            rows.append(_row(day, "path", path, views))
    total = sum(row["pageviews"] for row in rows)
    for dimension, shares in SPLITS.items():
        rows.extend(_row(day, dimension, value, round(total * share)) for value, share in shares)
    return [row for row in rows if row["pageviews"]]


def _row(day, dimension, value, views):
    return {
        "date": day.isoformat(),
        "dimension": dimension,
        "value": value,
        "pageviews": views,
        "visits": max(1, round(views * 0.72)) if views else 0,
        "sample_interval": 1,
    }


def _vitals(day):
    beacons = 40 + int(hexed("vitals", day), 16) % 30
    return {
        "date": day.isoformat(),
        "largest_contentful_paint_ms": 1_180 + int(hexed("lcp", day), 16) % 400,
        "interaction_to_next_paint_ms": 64 + int(hexed("inp", day), 16) % 60,
        "first_contentful_paint_ms": 620 + int(hexed("fcp", day), 16) % 200,
        "time_to_first_byte_ms": 140 + int(hexed("ttfb", day), 16) % 90,
        "cumulative_layout_shift": Decimal("0.0200"),
        "lcp_good": beacons - 3,
        "lcp_needs_improvement": 2,
        "lcp_poor": 1,
        "inp_good": beacons - 1,
        "inp_needs_improvement": 1,
        "inp_poor": 0,
        "cls_good": beacons,
        "cls_needs_improvement": 0,
        "cls_poor": 0,
        "sample_interval": 1,
    }


def analytics():
    """A quarter of the site's traffic, recorded as the controller reports it."""

    start, end = completed_window(BACKFILL_DAYS)
    today = timezone.localdate()
    days = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
    record_analytics(
        {
            "sites": [
                {
                    "site_tag": hexed("site", ZONE, length=32),
                    "host": ZONE,
                    "connection_ref": CLOUDFLARE,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "rows": [row for day in days for row in _day(day, (today - day).days)],
                    "vitals": [_vitals(day) for day in days],
                }
            ]
        },
        principal=cli_principal(),
    )


# Who wrote, what about, how long ago, and what became of it.
MESSAGES = (
    (
        "John Smith",
        "john.smith@example.org",
        "US",
        timedelta(hours=5),
        "read",
        "Read the post on restoring backups. Do you test the restore against a fresh volume or the live one?",
    ),
    (
        "Mary Johnson",
        "mary.johnson@example.net",
        "US",
        timedelta(days=3),
        "replied",
        "Thanks for the writeup on mirroring a private git server. Which mirror schedule ended up holding?",
    ),
    (
        "Mike Davis",
        "mike.davis@example.com",
        "US",
        timedelta(days=11),
        "read",
        "Would you share the compose file for the media server? The TV discovery part is what I am stuck on.",
    ),
    (
        "Rank Booster",
        "seo@example.net",
        "US",
        timedelta(days=16),
        "spam",
        "We can put your site on the first page of every search engine. Reply for a free audit.",
    ),
)


def contact():
    """The contact form's messages, as the controller read them from D1."""

    def message(number, sender, email, country, age, status, text):
        at = (timezone.now() - age).strftime("%Y-%m-%d %H:%M:%S")
        return {
            "connection_ref": CLOUDFLARE,
            "account_id": ACCOUNT,
            "database_id": DATABASE_ID,
            "database": DATABASE,
            "id": number,
            "created_at": at,
            "updated_at": at,
            "name": sender,
            "email": email,
            "message": text,
            "status": status,
            "country": country,
            "browser": "Firefox",
            "device": "desktop",
            "source_url": f"{SITE}/contact/",
            "turnstile": "verified",
        }

    store(
        CONTACT_SUBMISSION_KIND,
        *(message(len(MESSAGES) - index, *fields) for index, fields in enumerate(MESSAGES)),
    )
    store(
        "cloudflare.d1_database",
        {
            "connection_ref": CLOUDFLARE,
            "account_id": ACCOUNT,
            "name": DATABASE,
            "uuid": DATABASE_ID,
            "created_at": iso(timedelta(days=400)),
            "version": "production",
            "file_size": 114_688,
        },
    )

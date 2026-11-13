"""What the operator keeps by hand: projects, runbooks, the calendar, and what
running the homelab costs."""

import struct
import zlib
from datetime import time, timedelta
from decimal import Decimal

from django.core.files.base import ContentFile
from django.utils import timezone

from hq.domains.projects.models import Project
from hq.domains.receipts.models import Receipt
lazy from hq.domains.assets.models import Asset
lazy from hq.domains.calendars.models import Entry
lazy from hq.domains.content.models import ContentItem
lazy from hq.domains.docs_index.models import DocumentationRecord
lazy from hq.domains.expenses.models import Expense

from .names import MACHINE
from .network import RENEWS_IN
from .work import UPDATED

# A one-page PDF that says what it is.
_RECEIPT = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 120]/Contents 4 0 R"
    b"/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
    b"4 0 obj<</Length 44>>stream\nBT /F1 14 Tf 20 60 Td (Example receipt) Tj ET\nendstream endobj\n"
    b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
)


def _png(width, height):
    """A slip of paper photographed, a line of print every eighth row, as a
    grayscale PNG written here so the repository holds no binary for it."""

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    paper, ink = b"\xff", b"\x60"
    rows = b"".join(
        b"\0" + ((ink * (width // 2)).center(width, paper) if row % 8 == 4 and 4 < row < height - 8 else paper * width)
        for row in range(height)
    )
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


# A receipt photographed rather than scanned, so a receipt's page draws its picture.
_PICTURE = _png(60, 80)
# Name, slug, repository, public address, what it is.
PROJECTS = (
    (
        "HQ",
        "hq",
        "hq",
        "https://hq.example.com/",
        "The control panel for the homelab: every machine, service, renewal and bill in one place.",
    ),
    (
        "Photo library",
        "photo-library",
        "photo-library",
        "https://photos.example.com/",
        "Every family photo in one place, backed up off site, instead of in three cloud accounts.",
    ),
    (
        "Media server",
        "media-server",
        "media-server",
        "https://streaming.example.com/",
        "Films and music streamed to the TV and the phone from the shelf in the hall.",
    ),
    (
        "Self-hosted git",
        "self-hosted-git",
        "git-server",
        "https://git.example.com/",
        "A git server for private work, mirrored to GitHub every night.",
    ),
    (
        "Tailnet hardening",
        "tailnet-hardening",
        "homelab",
        "",
        "Every service reachable over the tailnet only, behind lock, with one grant per thing that needs it.",
    ),
    (
        "Offsite backups",
        "offsite-backups",
        "backups",
        "",
        "A nightly encrypted copy of the server's volumes to cloud storage, restored once a quarter to prove it works.",
    ),
)
# What each project is built with, where its operator wrote it down.
STACKS = {
    "hq": "Django, SQLite, Go",
    "photo-library": "Immich, PostgreSQL",
    "tailnet-hardening": "Debian, systemd, Caddy, Tailscale",
}
# Slug, title, the project it writes up, how long ago it was published.
WRITEUPS = (
    ("why-a-control-panel", "Why the homelab has a control panel", "hq", 40),
    ("photos-off-the-cloud", "Moving forty thousand photos off the cloud", "photo-library", 150),
    ("media-server-on-the-tv", "A media server the TV finds by itself", "media-server", 210),
    ("mirroring-private-git", "Mirroring a private git server to GitHub", "self-hosted-git", 95),
    ("tailnet-only", "Closing every port but the tailnet", "tailnet-hardening", 60),
    ("restored-backups", "Backups you have actually restored", "offsite-backups", 25),
)
# doc_id, title, environment, the projects it is about, and how many days ago
# it was first written and last reviewed.
RUNBOOKS = (
    (
        "rb-restore-backup",
        "Restore a volume from the offsite backup",
        "homelab",
        ("offsite-backups", "photo-library"),
        125,
        20,
    ),
    ("rb-rotate-tailnet-keys", "Rotate the tailnet lock signing keys", "tailscale", ("tailnet-hardening",), 140, 45),
    # Past the review interval: the one runbook due for review.
    ("rb-public-site", "Publish the public site and answer its contact form", "homelab", ("hq",), 380, 200),
    (
        "rb-upgrade-containers",
        "Upgrade a container to a new release",
        "homelab",
        ("media-server", "self-hosted-git", "hq"),
        290,
        9,
    ),
)


def projects():
    for name, slug, repository, url, about in PROJECTS:
        Project.objects.create(
            name=name,
            slug=slug,
            category="homelab",
            status="active",
            public_url=url,
            description=about,
            technologies_used=STACKS.get(slug, ""),
            repository_url=f"https://github.com/example/{repository}",
            dependency_updates=repository == UPDATED,
        )
    _runbooks()
    _writeups()


def _runbooks():
    today = timezone.localdate()
    for doc_id, title, environment, about, written, reviewed in RUNBOOKS:
        record = _about(
            DocumentationRecord,
            about,
            doc_id=doc_id,
            title=title,
            doc_type="runbook",
            environment=environment,
            status="active",
            sensitivity="internal",
            last_reviewed=today - timedelta(days=written),
        )
        # Reviewed since, and mirrored again.
        record.last_reviewed = today - timedelta(days=reviewed)
        record.save()


def _writeups():
    today = timezone.localdate()
    for slug, title, project, days in WRITEUPS:
        item = _about(
            ContentItem,
            (project,),
            title=title,
            slug=slug,
            content_type="lab_writeup",
            status="published",
            published_url=f"https://example.com/writeups/{slug}/",
            published_at=today - timedelta(days=days),
        )
        # Each writeup cites the runbooks of the project it is about.
        item.related_documentation.set(DocumentationRecord.objects.filter(related_projects__slug=project))


def _about(model, projects: tuple[str, ...], **fields):
    """A record of ``model``, written about the projects named by slug."""

    record = model.objects.create(**fields)
    record.related_projects.set(Project.objects.filter(slug__in=projects))
    return record


def calendar():
    today = timezone.localdate()
    Entry.objects.create(
        title="Restore test from offsite backup", starts_on=today + timedelta(days=2), starts_at=time(19)
    )
    Entry.objects.create(
        title="Patch the host", starts_on=today - timedelta(weeks=26), repeat="weekly", weekdays="5", starts_at=time(9)
    )
    Entry.objects.create(title="Renew example.com", starts_on=today + RENEWS_IN, repeat="yearly")


# The server, bought refurbished just before it was declared in HQ
# (``history.MACHINE_DAYS``): how long ago, from whom, what, its expense
# category, cost, and how much of its use is the business's.
# The server as an asset, by its slug.
SERVER_ASSET = "sff-server"
SERVER = (430, "eBay", "Small form factor server", "hardware", "389.00", 80)
# How long ago each monthly storage bill was paid since the backups started.
STORAGE = (2, 32, 62, 92, 122)
# What running the host has cost, each filed with its receipt: an expense's
# fields, whether it bought the server, and the project it went to by slug.
SPENDING = (
    (*SERVER, True, None),
    (120, "Newegg", "2 TB backup drive", "hardware", "79.99", 50, False, "offsite-backups"),
    # Renewed a year before the registration next runs out.
    (365 - RENEWS_IN.days, "Cloudflare", "example.com renewal", "domains", "10.44", 100, False, None),
    # Storage is billed monthly; the backups hold family photos as well as the lab's volumes.
    *(
        (days, "Backblaze", "Offsite backup storage", "hosting", "6.00", 50, False, "offsite-backups")
        for days in STORAGE
    ),
    # A refund is a negative cost.
    (100, "Newegg", "2 TB backup drive return", "hardware", "-79.99", 50, False, "offsite-backups"),
)
# A bill paid this week whose receipt was photographed, dropped in and not yet
# filed against it, so the receipt's match page offers the expense of its
# amount and its own page draws the picture. The receipt follows every filed
# one, numbered as an empty database numbers them.
UNFILED = (3, "APC", "Replacement UPS battery", "hardware", "54.99", 80)
UNFILED_RECEIPT = len(SPENDING) + 1
# A switch handed down by a friend: no cost, no expense, nothing written about
# it and no record beside it, so its page is its facts alone.
SWITCH_ASSET = "hallway-switch"
SWITCH = (300, "8-port gigabit switch", "networking")
# More gear handed down with no cost written, so the queue's item for assets
# without a cost names three, each a button, and its row holds more buttons
# than its line shows.
HANDED_DOWN = (
    (240, "SSD enclosure", "server_hardware"),
    (180, "USB microphone", "camera_audio"),
)


def money():
    """The host as an asset, and what running it has cost, every expense with
    its receipt but this week's, a switch nothing else is about, and more gear
    handed down."""

    machine = f"machine:{MACHINE}"
    today = timezone.localdate()
    asset = Asset.objects.create(
        item_name=SERVER[2],
        slug=SERVER_ASSET,
        vendor=SERVER[1],
        category="server_hardware",
        purchase_date=today - timedelta(days=SERVER[0]),
        total_cost=Decimal(SERVER[4]),
        business_use_percentage=SERVER[5],
        payment_method="credit",
        infrastructure=machine,
        infrastructure_name=MACHINE,
    )
    asset.related_projects.set(Project.objects.exclude(slug="offsite-backups"))
    for *fields, bought, project in SPENDING:
        expense = _expense(*fields, project=project, asset=asset if bought else None)
        _receipt(expense.vendor, expense.date, expense.total_cost, related_expense=expense)
    expense = _expense(*UNFILED)
    _receipt(expense.vendor, expense.date, expense.total_cost, picture=True)
    days, name, category = SWITCH
    Asset.objects.create(
        item_name=name, slug=SWITCH_ASSET, category=category, purchase_date=today - timedelta(days=days)
    )
    for days, name, category in HANDED_DOWN:
        Asset.objects.create(item_name=name, category=category, purchase_date=today - timedelta(days=days))


def _expense(days, vendor, item, category, cost, business_use, *, project=None, asset=None):
    machine = f"machine:{MACHINE}"
    return Expense.objects.create(
        date=timezone.localdate() - timedelta(days=days),
        vendor=vendor,
        item=item,
        category=category,
        total_cost=Decimal(cost),
        business_use_percentage=business_use,
        payment_method="credit",
        related_asset=asset,
        related_project=Project.objects.get(slug=project) if project else None,
        about=machine if asset else "",
        about_name=MACHINE if asset else "",
    )


def _receipt(vendor, date, amount, *, picture=False, **filed):
    content, extension, kind = (_PICTURE, "png", "image/png") if picture else (_RECEIPT, "pdf", "application/pdf")
    return Receipt.objects.create(
        file=ContentFile(content, name=f"receipt-{vendor.lower()}-{date.isoformat()}.{extension}"),
        original_filename=f"{vendor} receipt.{extension}",
        content_type=kind,
        size_bytes=len(content),
        vendor=vendor,
        date=date,
        amount=amount,
        **filed,
    )

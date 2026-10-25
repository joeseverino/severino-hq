"""What the operator keeps by hand: projects, runbooks, the calendar, and what
running the homelab costs."""

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
    ("rb-public-site", "Publish the public site and answer its contact form", "homelab", ("hq",), 380, 30),
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
            repository_url=f"https://github.com/example/{repository}",
            dependency_updates=repository == UPDATED,
        )
    _runbooks()
    _writeups()


def _runbooks():
    today = timezone.localdate()
    for doc_id, title, environment, about, written, reviewed in RUNBOOKS:
        record = DocumentationRecord.objects.create(
            doc_id=doc_id,
            title=title,
            doc_type="runbook",
            environment=environment,
            status="active",
            sensitivity="internal",
            last_reviewed=today - timedelta(days=written),
        )
        record.related_projects.set(Project.objects.filter(slug__in=about))
        # Reviewed since, and mirrored again.
        record.last_reviewed = today - timedelta(days=reviewed)
        record.save()


def _writeups():
    today = timezone.localdate()
    for slug, title, project, days in WRITEUPS:
        item = ContentItem.objects.create(
            title=title,
            slug=slug,
            content_type="lab_writeup",
            status="published",
            published_url=f"https://example.com/writeups/{slug}/",
            published_at=today - timedelta(days=days),
        )
        item.related_projects.set(Project.objects.filter(slug=project))
        # Each writeup cites the runbooks of the project it is about.
        item.related_documentation.set(DocumentationRecord.objects.filter(related_projects__slug=project))


def calendar():
    today = timezone.localdate()
    Entry.objects.create(
        title="Restore test from offsite backup", starts_on=today + timedelta(days=2), starts_at=time(19)
    )
    Entry.objects.create(
        title="Patch the host", starts_on=today - timedelta(weeks=26), repeat="weekly", weekdays="5", starts_at=time(9)
    )
    Entry.objects.create(title="Renew example.com", starts_on=today + RENEWS_IN, repeat="yearly")


# The server, bought refurbished: how long ago, from whom, what, its expense
# category, cost, and how much of its use is the business's.
SERVER = (200, "eBay", "Small form factor server", "hardware", "389.00", 80)
# How long ago each monthly storage bill was paid since the backups started.
STORAGE = (2, 32, 62, 92, 122)


def money():
    """The host as an asset, and a year of what running it cost, every expense with its receipt."""

    machine = f"machine:{MACHINE}"
    today = timezone.localdate()
    asset = Asset.objects.create(
        item_name=SERVER[2],
        slug="sff-server",
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
    backups = Project.objects.get(slug="offsite-backups")
    for days, vendor, item, category, cost, business_use, about, project in (
        (*SERVER, asset, None),
        (120, "Newegg", "2 TB backup drive", "hardware", "79.99", 50, None, backups),
        # Renewed a year before the registration next runs out.
        (365 - RENEWS_IN.days, "Cloudflare", "example.com renewal", "domains", "10.44", 100, None, None),
        # Storage is billed monthly; the backups hold family photos as well as the lab's volumes.
        *((days, "Backblaze", "Offsite backup storage", "hosting", "6.00", 50, None, backups) for days in STORAGE),
        # A refund is a negative cost.
        (100, "Newegg", "2 TB backup drive return", "hardware", "-79.99", 50, None, backups),
    ):
        expense = Expense.objects.create(
            date=today - timedelta(days=days),
            vendor=vendor,
            item=item,
            category=category,
            total_cost=Decimal(cost),
            business_use_percentage=business_use,
            payment_method="credit",
            related_asset=about,
            related_project=project,
            about=machine if about else "",
            about_name=MACHINE if about else "",
        )
        Receipt.objects.create(
            file=ContentFile(_RECEIPT, name=f"receipt-{expense.pk}.pdf"),
            original_filename=f"{vendor} receipt.pdf",
            content_type="application/pdf",
            vendor=vendor,
            date=expense.date,
            amount=expense.total_cost,
            related_expense=expense,
        )

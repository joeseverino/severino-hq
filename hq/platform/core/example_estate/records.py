"""What the operator keeps by hand: projects, runbooks, the calendar, and what
running the homelab costs."""

from datetime import date, time, timedelta
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
    ("HQ", "hq", "hq", "https://hq.example.com/", "The control panel for the homelab, and the site this demo is."),
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
        "https://media.example.com/",
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
# doc_id, title, environment, the projects it is about.
RUNBOOKS = (
    ("rb-restore-backup", "Restore a volume from the offsite backup", "homelab", ("offsite-backups", "photo-library")),
    ("rb-rotate-tailnet-keys", "Rotate the tailnet lock signing keys", "tailscale", ("tailnet-hardening",)),
    (
        "rb-upgrade-containers",
        "Upgrade a container to a new release",
        "homelab",
        ("media-server", "self-hosted-git", "hq"),
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
        )
    _runbooks()
    _writeups()


def _runbooks():
    reviewed = timezone.localdate() - timedelta(days=20)
    for doc_id, title, environment, about in RUNBOOKS:
        record = DocumentationRecord.objects.create(
            doc_id=doc_id,
            title=title,
            doc_type="runbook",
            environment=environment,
            status="active",
            sensitivity="internal",
            last_reviewed=reviewed,
        )
        record.related_projects.set(Project.objects.filter(slug__in=about))


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
    Entry.objects.create(title="Patch the host", starts_on=today, repeat="weekly", weekdays="5", starts_at=time(9))
    Entry.objects.create(title="Renew example.com", starts_on=today + RENEWS_IN, repeat="yearly")


def money():
    """The host as an asset, and a year of what running it cost, every expense with its receipt."""

    machine = f"machine:{MACHINE}"
    asset = Asset.objects.create(
        item_name="Small form factor server",
        slug="example-server",
        purchase_date=date(2025, 3, 14),
        total_cost=Decimal("389.00"),
        infrastructure=machine,
        infrastructure_name=MACHINE,
    )
    asset.related_projects.set(Project.objects.exclude(slug="offsite-backups"))
    backups = Project.objects.get(slug="offsite-backups")
    today = timezone.localdate()
    for days, vendor, item, category, cost, about, project in (
        (200, "Example Hardware", "Small form factor server", "hardware", "389.00", asset, None),
        (120, "Example Storage", "2 TB backup drive", "hardware", "79.99", None, backups),
        (60, "Example Registrar", "example.com renewal", "domains", "10.44", None, None),
        (30, "Example Cloud", "Offsite backup storage", "hosting", "6.00", None, backups),
        (2, "Example Cloud", "Offsite backup storage", "hosting", "6.00", None, backups),
    ):
        expense = Expense.objects.create(
            date=today - timedelta(days=days),
            vendor=vendor,
            item=item,
            category=category,
            total_cost=Decimal(cost),
            related_asset=about,
            related_project=project,
            about=machine if about else "",
            about_name=MACHINE if about else "",
        )
        Receipt.objects.create(
            file=ContentFile(_RECEIPT, name=f"example-{expense.pk}.pdf"),
            original_filename=f"{vendor} receipt.pdf",
            content_type="application/pdf",
            vendor=vendor,
            date=expense.date,
            amount=expense.total_cost,
            related_expense=expense,
        )

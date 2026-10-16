"""Seed Severino HQ with a small, plausible set of demo records.

Idempotent: re-running will skip records that already exist (matched by slug
or doc_id). Safe to run on a fresh DB or one with existing data.

    python manage.py seed_demo
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, override

from django.core.management.base import BaseCommand
from django.db import models, transaction
from django.utils import timezone

from hq.domains.assets.models import Asset
from hq.domains.content.models import ContentItem
from hq.domains.docs_index.models import DocumentationRecord
from hq.domains.expenses.models import Expense
from hq.domains.projects.models import Project


def _seed[Record: models.Model](
    model: type[Record], key: str, plan: list[dict[str, Any]], **related: dict[str, models.Model]
) -> dict[str, Record]:
    """Each planned record by its ``key``, created only where none has it.

    ``related`` names a many-to-many field and the records its planned keys
    may point at; a record already there keeps the links it has.
    """

    seeded: dict[str, Record] = {}
    for entry in plan:
        links = {field: entry.pop(field, ()) for field in related}
        record, created = model._default_manager.get_or_create(**{key: entry[key]}, defaults=entry)
        if created:
            for field, keys in links.items():
                getattr(record, field).set(related[field][name] for name in keys if name in related[field])
        seeded[entry[key]] = record
    return seeded


class Command(BaseCommand):
    help = "Populate the database with demo projects, content, docs, assets, expenses."

    @override
    @transaction.atomic
    def handle(self, *args, **options):
        today = timezone.localdate()
        year = today.year

        projects = self._seed_projects(today)
        assets = self._seed_assets(today)
        content = self._seed_content(today, projects=projects, assets=assets)
        docs = self._seed_docs(today, projects=projects, assets=assets)
        expenses = self._seed_expenses(year=year, projects=projects, assets=assets, docs=docs, content=content)

        if not options["verbosity"]:
            return
        self.stdout.write(
            self.style.SUCCESS(
                f"Demo seed complete: "
                f"{len(projects)} projects, {len(content)} content, "
                f"{len(docs)} docs, {len(assets)} assets, {len(expenses)} expenses."
            )
        )

    def _seed_projects(self, today: date) -> dict[str, Project]:
        plan = [
            {
                "slug": "homelab-dns",
                "name": "Homelab DNS · AdGuard + Unbound",
                "category": "homelab",
                "status": Project.Status.ACTIVE,
                "description": "Filtering and recursion for the homelab LAN.",
                "technologies_used": "AdGuard Home, Unbound, Tailscale",
            },
            {
                "slug": "vps-baseline",
                "name": "VPS baseline hardening",
                "category": "vps",
                "status": Project.Status.ACTIVE,
                "description": "Baseline systemd / Caddy / Tailscale-only VPS setup.",
                "technologies_used": "Debian, systemd, Caddy, Tailscale",
            },
            {
                "slug": "wp-security-pass",
                "name": "WordPress security pass · example.com",
                "category": "wordpress_security",
                "status": Project.Status.ACTIVE,
                "description": "Lockdown pass on the public WordPress site.",
                "technologies_used": "Cloudflare, WordPress",
            },
        ]
        return _seed(Project, "slug", plan)

    def _seed_assets(self, today: date) -> dict[str, Asset]:
        plan = [
            {
                "slug": "mini-pc",
                "item_name": "Small-form-factor PC (homelab host)",
                "vendor": "Dell (refurb)",
                "category": "server_hardware",
                "purchase_date": today - timedelta(days=120),
                "total_cost": Decimal("220.00"),
                "business_use_percentage": 100,
                "status": Asset.Status.ACTIVE,
            },
            {
                "slug": "unifi-flex-mini",
                "item_name": "UniFi Flex Mini switch",
                "vendor": "Ubiquiti",
                "category": "networking",
                "purchase_date": today - timedelta(days=60),
                "total_cost": Decimal("29.00"),
                "business_use_percentage": 100,
                "status": Asset.Status.ACTIVE,
            },
            {
                "slug": "yubikey-5c",
                "item_name": "YubiKey 5C NFC",
                "vendor": "Yubico",
                "category": "tools",
                "purchase_date": today - timedelta(days=200),
                "total_cost": Decimal("55.00"),
                "business_use_percentage": 100,
                "status": Asset.Status.ACTIVE,
            },
        ]
        return _seed(Asset, "slug", plan)

    def _seed_content(
        self, today: date, *, projects: dict[str, Project], assets: dict[str, Asset]
    ) -> dict[str, ContentItem]:
        plan = [
            {
                "slug": "homelab-dns-writeup",
                "title": "How I run AdGuard + Unbound on a $220 mini PC",
                "content_type": ContentItem.Type.ARTICLE,
                "status": ContentItem.Status.DRAFT,
                "topic": "Homelab DNS",
                "tags": "homelab, dns, adguard, unbound",
                "related_projects": ["homelab-dns"],
                "related_assets": ["mini-pc", "unifi-flex-mini"],
            },
            {
                "slug": "vps-tailscale-only",
                "title": "Tailnet-only VPS: public name, private surface",
                "content_type": ContentItem.Type.GUIDE,
                "status": ContentItem.Status.DRAFT,
                "topic": "VPS / Tailscale",
                "tags": "tailscale, vps, caddy, hardening",
                "related_projects": ["vps-baseline"],
            },
        ]
        return _seed(ContentItem, "slug", plan, related_projects=projects, related_assets=assets)

    def _seed_docs(
        self, today: date, *, projects: dict[str, Project], assets: dict[str, Asset]
    ) -> dict[str, DocumentationRecord]:
        plan = [
            {
                "doc_id": "rb-adguard-001",
                "title": "AdGuard Home runbook",
                "doc_type": DocumentationRecord.DocType.RUNBOOK,
                "system_service": "AdGuard Home",
                "environment": DocumentationRecord.Environment.HOMELAB,
                "status": DocumentationRecord.Status.ACTIVE,
                "sensitivity": DocumentationRecord.Sensitivity.SENSITIVE,
                "obsidian_path": "Infra/DNS/AdGuard Home.md",
                "last_reviewed": today - timedelta(days=20),
                "related_projects": ["homelab-dns"],
                "related_assets": ["mini-pc"],
            },
            {
                "doc_id": "dr-vps-tailnet-001",
                "title": "VPS Tailnet-only deployment guide",
                "doc_type": DocumentationRecord.DocType.DEPLOYMENT_GUIDE,
                "system_service": "VPS",
                "environment": DocumentationRecord.Environment.VPS,
                "status": DocumentationRecord.Status.ACTIVE,
                "sensitivity": DocumentationRecord.Sensitivity.INTERNAL,
                "obsidian_path": "Infra/VPS/Tailnet-only Deploy.md",
                "github_path": "infra-notes/vps-tailnet.md",
                "last_reviewed": today - timedelta(days=210),
                "related_projects": ["vps-baseline"],
            },
        ]
        return _seed(DocumentationRecord, "doc_id", plan, related_projects=projects, related_assets=assets)

    def _seed_expenses(
        self,
        *,
        year: int,
        projects: dict[str, Project],
        assets: dict[str, Asset],
        docs: dict[str, DocumentationRecord],
        content: dict[str, ContentItem],
    ) -> list[Expense]:
        records = [
            {
                "date": date(year, 1, 12),
                "vendor": "Cloudflare",
                "item": "Cloudflare Pro · example.com",
                "category": "hosting",
                "total_cost": Decimal("240.00"),
                "business_use_percentage": 100,
                "business_purpose": "Public site WAF + caching.",
                "related_project": projects.get("wp-security-pass"),
            },
            {
                "date": date(year, 2, 3),
                "vendor": "Namecheap",
                "item": "Domain renewal · example.com",
                "category": "domains",
                "total_cost": Decimal("14.99"),
                "business_use_percentage": 100,
                "business_purpose": "Brand domain.",
            },
            {
                "date": date(year, 3, 18),
                "vendor": "Ubiquiti",
                "item": "UniFi Flex Mini switch",
                "category": "networking",
                "total_cost": Decimal("29.00"),
                "business_use_percentage": 100,
                "business_purpose": "Homelab switching for lab/content workstations.",
                "related_asset": assets.get("unifi-flex-mini"),
                "related_project": projects.get("homelab-dns"),
            },
        ]
        out: list[Expense] = []
        for entry in records:
            obj, _ = Expense.objects.get_or_create(
                date=entry["date"],
                vendor=entry["vendor"],
                item=entry["item"],
                defaults=entry,
            )
            out.append(obj)
        return out

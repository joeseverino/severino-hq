"""Seed Severino HQ with a small, plausible set of demo records.

Idempotent: re-running will skip records that already exist (matched by slug
or doc_id). Safe to run on a fresh DB or one with existing data.

    python manage.py seed_demo
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from assets.models import Asset
from content.models import ContentItem
from docs_index.models import DocumentationRecord
from expenses.models import Expense
from projects.models import Project


class Command(BaseCommand):
    help = "Populate the database with demo projects, content, docs, assets, expenses."

    def handle(self, *args, **options):
        today = timezone.localdate()
        year = today.year

        projects = self._seed_projects(today)
        assets = self._seed_assets(today)
        content = self._seed_content(today, projects=projects, assets=assets)
        docs = self._seed_docs(today, projects=projects, assets=assets)
        expenses = self._seed_expenses(
            year=year, projects=projects, assets=assets, docs=docs, content=content
        )

        self.stdout.write(self.style.SUCCESS(
            f"Demo seed complete: "
            f"{len(projects)} projects, {len(content)} content, "
            f"{len(docs)} docs, {len(assets)} assets, {len(expenses)} expenses."
        ))

    def _seed_projects(self, today: date) -> dict[str, Project]:
        plan = [
            {
                "slug": "homelab-dns",
                "name": "Homelab DNS — AdGuard + Unbound",
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
                "name": "WordPress security pass — jseverino.com",
                "category": "wordpress_security",
                "status": Project.Status.ACTIVE,
                "description": "Lockdown pass on the public WordPress site.",
                "technologies_used": "Cloudflare, WordPress",
            },
        ]
        out: dict[str, Project] = {}
        for entry in plan:
            obj, _ = Project.objects.get_or_create(
                slug=entry["slug"], defaults=entry
            )
            out[entry["slug"]] = obj
        return out

    def _seed_assets(self, today: date) -> dict[str, Asset]:
        plan = [
            {
                "slug": "optiplex-7050",
                "item_name": "Dell OptiPlex 7050 (homelab host)",
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
        out: dict[str, Asset] = {}
        for entry in plan:
            obj, _ = Asset.objects.get_or_create(slug=entry["slug"], defaults=entry)
            out[entry["slug"]] = obj
        return out

    def _seed_content(
        self, today: date, *, projects: dict[str, Project], assets: dict[str, Asset]
    ) -> dict[str, ContentItem]:
        plan = [
            {
                "slug": "homelab-dns-writeup",
                "title": "How I run AdGuard + Unbound on a $220 OptiPlex",
                "content_type": ContentItem.Type.ARTICLE,
                "status": ContentItem.Status.DRAFT,
                "topic": "Homelab DNS",
                "tags": "homelab, dns, adguard, unbound",
                "_related_projects": ["homelab-dns"],
                "_related_assets": ["optiplex-7050", "unifi-flex-mini"],
            },
            {
                "slug": "vps-tailscale-only",
                "title": "Tailnet-only VPS — public name, private surface",
                "content_type": ContentItem.Type.GUIDE,
                "status": ContentItem.Status.DRAFT,
                "topic": "VPS / Tailscale",
                "tags": "tailscale, vps, caddy, hardening",
                "_related_projects": ["vps-baseline"],
            },
        ]
        out: dict[str, ContentItem] = {}
        for entry in plan:
            rel_projects = entry.pop("_related_projects", [])
            rel_assets = entry.pop("_related_assets", [])
            obj, created = ContentItem.objects.get_or_create(
                slug=entry["slug"], defaults=entry
            )
            if created:
                obj.related_projects.set(projects[s] for s in rel_projects if s in projects)
                obj.related_assets.set(assets[s] for s in rel_assets if s in assets)
            out[entry["slug"]] = obj
        return out

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
                "_projects": ["homelab-dns"],
                "_assets": ["optiplex-7050"],
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
                "_projects": ["vps-baseline"],
            },
        ]
        out: dict[str, DocumentationRecord] = {}
        for entry in plan:
            rel_projects = entry.pop("_projects", [])
            rel_assets = entry.pop("_assets", [])
            obj, created = DocumentationRecord.objects.get_or_create(
                doc_id=entry["doc_id"], defaults=entry
            )
            if created:
                obj.related_projects.set(projects[s] for s in rel_projects if s in projects)
                obj.related_assets.set(assets[s] for s in rel_assets if s in assets)
            out[entry["doc_id"]] = obj
        return out

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
                "item": "Cloudflare Pro — jseverino.com",
                "category": "hosting",
                "total_cost": Decimal("240.00"),
                "business_use_percentage": 100,
                "business_purpose": "Public site WAF + caching.",
                "related_project": projects.get("wp-security-pass"),
            },
            {
                "date": date(year, 2, 3),
                "vendor": "Namecheap",
                "item": "Domain renewal — jseverino.com",
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

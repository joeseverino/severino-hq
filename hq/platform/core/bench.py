"""A few years of one operator's records, for measuring what a page costs.

An empty database hides every cost that grows with data. ``seed`` fills one
with a deterministic estate: the ledger, the registries, a swept
infrastructure and its history. ``manage.py bench_pages`` times pages over it
and the query-budget tests pin counts against it at a smaller ``scale``.

Everything is synthetic: ``example.*`` names and documentation addresses.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from django.contrib.auth import get_user_model
from django.utils import timezone

from hq.domains.analytics.models import AnalyticsCoverage, AnalyticsSite, RumDaily, VitalsDaily
from hq.domains.assets.models import Asset
from hq.domains.calendars.models import Entry
from hq.domains.content.models import ContentItem
from hq.domains.control_plane.models import (
    ApprovalRequest,
    ManagedResource,
    OperationRequest,
    ProviderConnection,
    ProviderInventory,
)
from hq.domains.docs_index.models import DocumentationRecord
from hq.domains.expenses.models import Expense
from hq.domains.jobs.models import Job
from hq.domains.projects.models import Project
from hq.domains.receipts.models import Receipt
from hq.platform.core.models import AuditLog
from hq.platform.search_index.services import rebuild_search_index

VENDORS = (
    "Example Hosting", "Example Registrar", "Example Cloud", "Example Hardware",
    "Example Software", "Example Office", "Example Networks", "Example Books",
    "Example Telecom", "Example Tools", "Example Power", "Example Storage",
)
ZONES = ("example.com", "example.net", "example.org")
EDGE_ADDRESS = "198.51.100.20"
CONTROLLER = "example-controller"


@dataclass(frozen=True)
class Seeded:
    """One of each record, for the pages that show a single one."""

    user: Any
    project: Project
    asset: Asset
    expense: Expense
    receipt: Receipt
    documentation: DocumentationRecord
    content: ContentItem
    entry: Entry
    resource: ManagedResource
    audit: AuditLog
    job: Job
    approval: ApprovalRequest


def _count(scale: float, full: int) -> int:
    return max(2, round(full * scale))


def _choices(model, field: str) -> list[str]:
    return [value for value, _ in model._meta.get_field(field).choices if value]


def _create(model, rows: list[Any]) -> list[Any]:
    model.objects.bulk_create(rows, batch_size=500)
    # SQLite returns no keys for a bulk insert of an explicit-key-less model on
    # every version, so read them back in insertion order.
    return list(model.objects.order_by("pk"))


def _spread(model, field: str, rows: list[Any], days: int) -> None:
    """Date rows across ``days``, oldest key first, as years of use would have.

    The fields are stamped on insert, which would put the whole history in the
    seed's own second and every row of it in one month of any calendar.
    """

    now = timezone.now()
    step = timedelta(days=days) / max(len(rows), 1)
    for index, row in enumerate(rows):
        setattr(row, field, now - step * (len(rows) - index))
    model.objects.bulk_update(rows, [field], batch_size=500)


def _projects(rng: random.Random, scale: float) -> list[Project]:
    categories = _choices(Project, "category")
    statuses = _choices(Project, "status")
    return _create(Project, [
        Project(
            name=f"Example project {index}",
            slug=f"example-project-{index}",
            category=categories[index % len(categories)],
            status=statuses[index % len(statuses)],
            description="A project in the bench estate.",
            technologies_used="Django, SQLite, Go",
            repository_url=f"https://example.com/repos/example-project-{index}",
        )
        for index in range(_count(scale, 40))
    ])


def _assets(rng: random.Random, scale: float, today: date, projects: list[Project]) -> list[Asset]:
    categories = _choices(Asset, "category")
    statuses = _choices(Asset, "status")
    assets = _create(Asset, [
        Asset(
            item_name=f"Example asset {index}",
            slug=f"example-asset-{index}",
            vendor=rng.choice(VENDORS),
            category=categories[index % len(categories)],
            purchase_date=today - timedelta(days=rng.randrange(1500)),
            total_cost=Decimal(rng.randrange(1000, 250000)) / 100,
            business_use_percentage=rng.choice((25, 50, 100)),
            estimated_deductible_amount=Decimal("10.00"),
            serial_number=f"SN-{index:05d}",
            warranty_date=today + timedelta(days=rng.randrange(-200, 700)),
            status=statuses[0] if index % 5 else statuses[index % len(statuses)],
        )
        for index in range(_count(scale, 300))
    ])
    through = Asset.related_projects.through
    through.objects.bulk_create([
        through(asset_id=asset.pk, project_id=projects[(index + step) % len(projects)].pk)
        for index, asset in enumerate(assets)
        for step in range(index % 3)
    ], batch_size=500)
    return assets


def _documentation(scale: float, today: date, projects, assets) -> list[DocumentationRecord]:
    doc_types = _choices(DocumentationRecord, "doc_type")
    environments = _choices(DocumentationRecord, "environment")
    statuses = _choices(DocumentationRecord, "status")
    sensitivities = _choices(DocumentationRecord, "sensitivity")
    records = _create(DocumentationRecord, [
        DocumentationRecord(
            doc_id=f"example-doc-{index}",
            title=f"Example runbook {index}",
            doc_type=doc_types[index % len(doc_types)],
            system_service=f"example-service-{index % 12}",
            environment=environments[index % len(environments)],
            status=statuses[index % len(statuses)],
            sensitivity=sensitivities[index % len(sensitivities)],
            obsidian_path=f"03 Runbooks/Example runbook {index}.md",
            last_reviewed=today - timedelta(days=index % 400),
        )
        for index in range(_count(scale, 150))
    ])
    for field, targets, column in (
        ("related_projects", projects, "project_id"),
        ("related_assets", assets, "asset_id"),
    ):
        through = getattr(DocumentationRecord, field).through
        through.objects.bulk_create([
            through(documentationrecord_id=record.pk, **{column: targets[(index + step) % len(targets)].pk})
            for index, record in enumerate(records)
            for step in range(index % 3)
        ], batch_size=500)
    return records


def _content(scale: float, today: date, projects, records) -> list[ContentItem]:
    types = _choices(ContentItem, "content_type")
    statuses = _choices(ContentItem, "status")
    items = _create(ContentItem, [
        ContentItem(
            title=f"Example writeup {index}",
            slug=f"example-writeup-{index}",
            content_type=types[index % len(types)],
            status=statuses[index % len(statuses)],
            topic="Operations",
            tags="example, bench",
            published_url=f"https://example.com/writeups/example-writeup-{index}/",
            published_at=today - timedelta(days=index * 9),
        )
        for index in range(_count(scale, 80))
    ])
    for field, targets, column in (
        ("related_projects", projects, "project_id"),
        ("related_documentation", records, "documentationrecord_id"),
    ):
        through = getattr(ContentItem, field).through
        through.objects.bulk_create([
            through(contentitem_id=item.pk, **{column: targets[(index + step) % len(targets)].pk})
            for index, item in enumerate(items)
            for step in range(index % 3)
        ], batch_size=500)
    return items


def _expenses(rng, scale, today, projects, assets, content, records) -> list[Expense]:
    categories = _choices(Expense, "category")
    methods = _choices(Expense, "payment_method")

    def related(index: int, every: int, targets: list[Any]):
        return targets[index % len(targets)] if index % every == 0 else None

    rows = []
    for index in range(_count(scale, 4000)):
        cost = Decimal(rng.randrange(500, 90000)) / 100
        share = rng.choice((50, 100, 100, 100))
        rows.append(Expense(
            date=today - timedelta(days=rng.randrange(4 * 365)),
            vendor=rng.choice(VENDORS),
            item=f"Example purchase {index}",
            category=categories[index % len(categories)],
            total_cost=cost,
            business_use_percentage=share,
            estimated_deductible_amount=(cost * share / 100).quantize(Decimal("0.01")),
            payment_method=methods[index % len(methods)],
            business_purpose="Operating the example estate",
            related_project=related(index, 2, projects),
            related_asset=related(index, 5, assets),
            related_content=related(index, 11, content),
            related_documentation=related(index, 13, records),
        ))
    return _create(Expense, rows)


def _receipts(rng, scale, expenses: list[Expense], assets: list[Asset]) -> list[Receipt]:
    rows = []
    for index in range(_count(scale, 3000)):
        expense = expenses[index % len(expenses)] if index % 10 else None
        rows.append(Receipt(
            file=f"receipts/example/receipt-{index}.pdf",
            original_filename=f"receipt-{index}.pdf",
            content_type="application/pdf",
            size_bytes=rng.randrange(20_000, 900_000),
            vendor=expense.vendor if expense else rng.choice(VENDORS),
            date=expense.date if expense else None,
            amount=expense.total_cost if expense else Decimal("12.00"),
            related_expense=expense,
            related_asset=assets[index % len(assets)] if index % 7 == 0 else None,
        ))
    receipts = _create(Receipt, rows)
    _spread(Receipt, "uploaded_at", receipts, 4 * 365)
    return receipts


def _entries(scale: float, today: date) -> list[Entry]:
    repeats = ("", "", "", "weekly", "monthly", "yearly")
    return _create(Entry, [
        Entry(
            title=f"Example appointment {index}",
            starts_on=today + timedelta(days=index % 240 - 120),
            location="Example office" if index % 4 == 0 else "",
            repeat=repeats[index % len(repeats)],
        )
        for index in range(_count(scale, 300))
    ])


def _history(rng, scale, user, expenses, assets) -> list[AuditLog]:
    actions = ("created", "updated", "updated", "viewed", "exported", "observed")
    subjects = [("expense", expense.pk, str(expense)) for expense in expenses[:200]]
    subjects += [("asset", asset.pk, str(asset)) for asset in assets[:100]]
    rows = []
    for index in range(_count(scale, 6000)):
        kind, key, label = subjects[index % len(subjects)]
        rows.append(AuditLog(
            user=user if index % 6 else None,
            action=actions[index % len(actions)],
            object_type=kind,
            object_id=str(key),
            object_repr=label[:200],
            operation_id=f"example-operation-{index}",
            connection="example-ssh" if index % 9 == 0 else "",
            message="Recorded by the bench seed.",
            metadata={"changes": {"notes": ["", "example"]}} if index % 3 == 0 else {},
        ))
    history = _create(AuditLog, rows)
    _spread(AuditLog, "created_at", history, 4 * 365)
    return history


def _jobs(scale: float, user) -> list[Job]:
    now = timezone.now()
    states = ("succeeded", "succeeded", "succeeded", "failed", "lost")
    Job.objects.bulk_create([
        Job(
            kind=f"example.job.{index % 6}",
            label=f"Example job {index}",
            state=states[index % len(states)],
            percent=100,
            actor="bench",
            requested_by=user,
            result={"imported": index},
            started_at=now - timedelta(hours=index),
            finished_at=now - timedelta(hours=index) + timedelta(seconds=30),
        )
        for index in range(_count(scale, 200))
    ], batch_size=500)
    jobs = list(Job.objects.all())
    _spread(Job, "created_at", jobs, 365)
    return jobs


def _analytics(rng, scale, today: date) -> None:
    now = timezone.now()
    dimensions = {
        "path": [f"/writeups/example-writeup-{index}/" for index in range(12)],
        "referrer": ["example.org", "example.net", "search.example.com"],
        "country": ["US", "CA", "DE", "GB"],
        "device": ["desktop", "mobile"],
        "browser": ["Firefox", "Safari", "Chrome"],
        "os": ["macOS", "iOS", "Linux"],
    }
    for host in ZONES[:2]:
        site = AnalyticsSite.objects.create(
            site_tag=f"tag-{host}", host=host, connection_ref="example-cloudflare_api",
            first_seen_at=now - timedelta(days=400), observed_at=now,
        )
        days = [today - timedelta(days=offset) for offset in range(_count(scale, 90))]
        AnalyticsCoverage.objects.bulk_create([AnalyticsCoverage(site=site, date=day) for day in days])
        RumDaily.objects.bulk_create([
            RumDaily(
                site=site, date=day, dimension=dimension, value=value,
                pageviews=rng.randrange(1, 400), visits=rng.randrange(1, 200),
                sample_interval=1, observed_at=now,
            )
            for day in days
            for dimension, values in dimensions.items()
            for value in values
        ], batch_size=500)
        VitalsDaily.objects.bulk_create([
            VitalsDaily(
                site=site, date=day, largest_contentful_paint_ms=1800,
                interaction_to_next_paint_ms=120, first_contentful_paint_ms=900,
                time_to_first_byte_ms=200, cumulative_layout_shift=Decimal("0.05"),
                lcp_good=90, inp_good=95, cls_good=97, sample_interval=1, observed_at=now,
            )
            for day in days
        ])


def _store(kind: str, records: list[dict[str, Any]]) -> None:
    ProviderInventory.objects.create(
        kind=kind, records=records, reachable=True, connected=True,
        observed_at=timezone.now(), controller_id=CONTROLLER,
    )


def _resource(kind: str, key: str, spec: dict[str, Any], now: datetime, **fields: Any) -> ManagedResource:
    return ManagedResource(
        key=key, kind=kind, spec=spec, observed_generation=1, last_observed_at=now,
        conditions=[{"type": "Ready", "status": True}], **fields,
    )


def _machines(now: datetime) -> tuple[list[ManagedResource], list[dict[str, Any]]]:
    hosts = [(f"lab-{index}", f"192.0.2.{10 + index}") for index in range(1, 7)]
    hosts.append(("edge-1", EDGE_ADDRESS))
    resources = [
        _resource("machine", name, {"name": name, "addresses": [address]}, now)
        for name, address in hosts
    ]
    resources += [
        _resource("tailscale.device", f"device-{name}", {"name": name, "connection_ref": "example-tailscale"}, now)
        for name, _ in hosts
    ]
    resources += [
        _resource("network", f"network-{index}", {"name": f"lan-{index}", "cidr": f"192.0.2.{index * 64}/26"}, now)
        for index in range(3)
    ]
    devices = [
        {
            "name": name, "online": index % 4 != 0, "tags": ["tag:server"], "addresses": [address],
            "last_seen": (now - timedelta(hours=index)).isoformat(),
            "key_expires": (now + timedelta(days=60 + index)).isoformat(),
        }
        for index, (name, address) in enumerate(hosts + [(f"phone-{n}", f"192.0.2.{200 + n}") for n in range(5)])
    ]
    return resources, devices


def _names(scale: float) -> list[tuple[str, str]]:
    return [
        (f"app{index}.{ZONES[index % len(ZONES)]}", f"lab-{index % 6 + 1}")
        for index in range(_count(scale, 120))
    ]


def _infrastructure(scale: float) -> list[ManagedResource]:
    now = timezone.now()
    resources, devices = _machines(now)
    names = _names(scale)
    address = {f"lab-{index}": f"192.0.2.{10 + index}" for index in range(1, 7)}
    records, proxies, rewrites, routes, containers = [], [], [], [], []
    for index, (name, host) in enumerate(names):
        zone = name.split(".", 1)[1]
        record = {
            "zone": zone, "name": name, "record_type": "A", "content": EDGE_ADDRESS,
            "proxied": index % 2 == 0, "ttl": 1,
        }
        records.append({**record, "connection_ref": "example-cloudflare_api"})
        resources.append(_resource("cloudflare.dns_record", f"record-{index}", record, now))
        container = {"host": host, "name": f"app{index}", "ports": [8000 + index], "state": "running",
                     "connection_ref": "example-portainer"}
        containers.append(container)
        if index % 4 == 0:
            proxy = {"domain_names": [name], "forward_scheme": "http",
                     "forward_host": address[host], "forward_port": 8000 + index}
            proxies.append({**proxy, "connection_ref": "example-npm"})
            resources.append(_resource("npm.proxy_host", f"proxy-{index}", proxy, now))
            rewrite = {"domain": name, "answer": address[host]}
            rewrites.append({**rewrite, "connection_ref": "example-adguard"})
            resources.append(_resource("adguard.rewrite", f"rewrite-{index}", rewrite, now))
        if index % 8 == 1:
            route = {"connection_ref": "example-edge", "domain": name, "upstream": f"{address[host]}:{8000 + index}"}
            routes.append(route)
            resources.append(_resource("caddy.route", f"route-{index}", route, now))
        if index % 3 == 0:
            resources.append(_resource(
                "portainer.container", f"container-{index}",
                {"connection_ref": "example-portainer", "host": host, "name": f"app{index}"}, now,
            ))
    for index, zone in enumerate(ZONES):
        resources.append(_resource(
            "cloudflare.zone", zone.replace(".", "-"), {"zone": zone, "connection_ref": "example-cloudflare_api"}, now,
        ))
        resources.append(_resource(
            "tls.certificate", f"certificate-{index}",
            {"certificate_name": f"example-cert-{index}", "domains": [f"*.{zone}"]}, now,
            status={"not_after": (now + timedelta(days=20 + 30 * index)).isoformat()},
        ))
    ManagedResource.objects.bulk_create(resources, batch_size=500)
    _store("tailscale.device", devices)
    _store("cloudflare.dns_record", records)
    _store("cloudflare.zone", [
        {"zone": zone, "connection_ref": "example-cloudflare_api",
         "registration": {"expires_at": (now + timedelta(days=200)).isoformat(), "auto_renew": True}}
        for zone in ZONES
    ])
    _store("cloudflare.edge_certificate", [
        {"connection_ref": "example-cloudflare_api", "zone": zone, "id": f"edge-{zone}",
         "hosts": [zone, f"*.{zone}"], "status": "active",
         "expires_on": (now + timedelta(days=60)).isoformat()}
        for zone in ZONES
    ])
    _store("npm.proxy_host", proxies)
    _store("adguard.rewrite", rewrites)
    _store("caddy.route", routes)
    _store("portainer.container", containers)
    _connections(now, address)
    return list(ManagedResource.objects.all())


def _connections(now: datetime, address: dict[str, str]) -> None:
    endpoints = [
        ("example-cloudflare_api", "cloudflare_api", "https://api.example.com"),
        ("example-tailscale", "tailscale", "https://api.example.net"),
        ("example-npm", "npm", "https://npm.example.com"),
        ("example-adguard", "adguard", "https://adguard.example.com"),
        ("example-portainer", "portainer", "https://portainer.example.com"),
        ("example-edge", "ssh", f"{EDGE_ADDRESS}:22"),
    ]
    endpoints += [(name, "ssh", f"{host}:22") for name, host in address.items()]
    ProviderConnection.objects.bulk_create([
        ProviderConnection(
            connection_ref=ref, controller_id=CONTROLLER, provider=provider, endpoint=endpoint,
            reachable=True, probed=True, manages=True, observed_at=now, reported_at=now,
        )
        for ref, provider, endpoint in endpoints
    ])


def _operations(scale: float, user, resources: list[ManagedResource]) -> None:
    now = timezone.now()
    OperationRequest.objects.bulk_create([
        OperationRequest(
            resource=resources[index % len(resources)],
            action="reconcile",
            state="succeeded" if index % 9 else "failed",
            requested_by=user,
            requested_actor="bench",
            requested_interface="web",
            idempotency_key=f"example-operation-{index}",
            result={"changed": False},
            completed_at=now - timedelta(hours=index),
        )
        for index in range(_count(scale, 300))
    ], batch_size=500)
    _spread(OperationRequest, "created_at", list(OperationRequest.objects.all()), 365)
    ApprovalRequest.objects.bulk_create([
        ApprovalRequest(
            capability="example.change",
            target=resources[index % len(resources)].key,
            resource_kind=resources[index % len(resources)].kind,
            resource_key=resources[index % len(resources)].key,
            content_fingerprint=f"{index:064x}",
            requested_actor="example-agent",
            requested_interface="api",
            state="pending" if index % 15 == 1 else "approved",
            expires_at=now + timedelta(days=1),
        )
        for index in range(_count(scale, 40))
    ])


def seed(scale: float = 1.0) -> Seeded:
    """Fill the current database; ``scale`` multiplies every count."""

    rng = random.Random(20261004)
    today = timezone.localdate()
    user = get_user_model().objects.create_superuser("bench", "bench@example.com")
    projects = _projects(rng, scale)
    assets = _assets(rng, scale, today, projects)
    records = _documentation(scale, today, projects, assets)
    content = _content(scale, today, projects, records)
    expenses = _expenses(rng, scale, today, projects, assets, content, records)
    receipts = _receipts(rng, scale, expenses, assets)
    entries = _entries(scale, today)
    history = _history(rng, scale, user, expenses, assets)
    jobs = _jobs(scale, user)
    _analytics(rng, scale, today)
    resources = _infrastructure(scale)
    _operations(scale, user, resources)
    rebuild_search_index()
    return Seeded(
        user=user,
        project=projects[0],
        asset=assets[0],
        expense=expenses[0],
        receipt=receipts[1],
        documentation=records[0],
        content=content[0],
        entry=entries[0],
        resource=next(resource for resource in resources if resource.kind == "npm.proxy_host"),
        audit=history[0],
        job=jobs[0],
        approval=ApprovalRequest.objects.filter(state="approved").order_by("pk")[0],
    )

"""A synthetic estate for the browser layout gate; every name is example.*.

Pages render through their real views and the test client, so a template or
view change reaches the gate without this module being edited. Only what no
view reads from the database is patched in: the dashboard's domain
contributors and its outward links, which come from installed extensions and
controller sweeps.
"""

from contextlib import ExitStack, contextmanager
from datetime import date, time, timedelta
from unittest import mock

from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, ProviderInventory
from hq.domains.projects.models import Project
from hq.platform.application.report_testing import report_connections
from hq.platform.application.security import cli_principal
from hq.platform.application.services import CONTAINER_KIND
from hq.platform.application.ui import (
    ActivityCalendar,
    CalendarDay,
    ChartSeries,
    DomainOverview,
    Kpi,
    stacked_bar_chart,
)
from hq.platform.core import seeds
from hq.platform.core.tests.browser.gate import answered

SERVICE = "app.example.com"
MACHINE = "app-host"
ADDRESS = "192.0.2.10"
# HQ's own name, the machine's tailnet address, and the device asking.
HQ = "hq.example.com"
TAILNET_ADDRESS = "100.64.0.10"
CALLER = "100.64.0.20"
PROXY = "172.18.0.2"


def _healthy(key, kind, spec):
    return ManagedResource.objects.create(
        key=key,
        kind=kind,
        spec=spec,
        conditions=[{"type": "Ready", "status": True, "reason": "", "message": ""}],
    )


def build_estate(user):
    """One machine, the service it serves, the credentials that reach it,
    owned by ``user``."""

    for index in range(6):
        Project.objects.create(
            name=f"Example project with a long descriptive name {index}",
            slug=f"example-project-{index}",
            category="homelab",
            status="active",
            public_url=f"https://project-{index}.example.com/",
            repository_url=f"https://github.com/example/project-{index}",
        )
    ManagedResource.objects.create(
        key=MACHINE,
        kind="machine",
        spec={"name": MACHINE, "role": "Docker host", "addresses": [ADDRESS, TAILNET_ADDRESS]},
    )
    _healthy("app-dns", "adguard.rewrite", {"domain": SERVICE, "answer": ADDRESS})
    _healthy("hq-dns", "adguard.rewrite", {"domain": HQ, "answer": TAILNET_ADDRESS})
    _healthy(
        "hq-proxy",
        "npm.proxy_host",
        {
            "domain_names": [HQ],
            "forward_scheme": "http",
            "forward_host": ADDRESS,
            "forward_port": 8000,
        },
    )
    _healthy(
        "app-proxy",
        "npm.proxy_host",
        {
            "domain_names": [SERVICE],
            "forward_scheme": "http",
            "forward_host": ADDRESS,
            "forward_port": 8000,
        },
    )
    # Other names for the service: each answers with the service's own name.
    for index in range(2):
        _healthy(
            f"app-alias-{index}",
            "cloudflare.dns_record",
            {
                "zone": "example.com",
                "name": f"another-name-for-the-application-with-a-long-name-{index}.example.com",
                "record_type": "CNAME",
                "content": SERVICE,
                "proxied": False,
                "ttl": 1,
            },
        )
    _healthy(
        "example-wildcard",
        "tls.certificate",
        {
            "certificate_name": "example-wildcard",
            "domains": ["example.com", "*.example.com"],
            "install_on": ["a-proxy"],
            "renewal_window_days": 30,
        },
    )
    report_connections(
        [
            {
                "connection_ref": "a-portainer",
                "provider": "portainer",
                "endpoint": "https://portainer.example.com",
                "reaches": [MACHINE, "another-host"],
                "ok": True,
                "probed": True,
                "detail": "2 of 2 environments reachable.",
            },
            {
                "connection_ref": "example-dns",
                "provider": "cloudflare_dns",
                "endpoint": "https://api.example.com/client/v4",
                "reaches": ["example.com", "example.net"],
                "ok": True,
                "probed": True,
                "detail": "2 zones.",
            },
            {
                "connection_ref": "example-npm",
                "provider": "npm",
                "endpoint": "https://proxy.example.com",
                "ok": False,
                "probed": True,
                "detail": "The address answered with a sign-in page at sso.example.com, "
                "not the API. Use the provider's direct API address.",
                "failure": "address",
            },
        ],
        principal=cli_principal(),
        controller_id="example-controller",
    )
    # A name only an Access application names: an observed row.
    ManagedResource.objects.create(
        key="example-zone",
        kind="cloudflare.zone",
        spec={"zone": "example.com", "connection_ref": "example-dns"},
    )
    ProviderInventory.objects.update_or_create(
        kind="cloudflare.access_app",
        defaults={
            "records": [{"id": "a1", "name": "Example admin", "domain": "admin.example.com"}],
            "reachable": True,
            "observed_at": timezone.now(),
        },
    )
    _tailnet()
    ProviderInventory.objects.update_or_create(
        kind=CONTAINER_KIND,
        defaults={
            "records": [
                {
                    "name": f"example-service-with-a-long-name-{index}",
                    "host": MACHINE,
                    "image": f"registry.example.com/example/image-{index}:1.2.3",
                    "state": "running" if index % 3 else "exited",
                    "ports": [8000 + index],
                }
                for index in range(8)
            ],
            "reachable": True,
            "observed_at": timezone.now(),
        },
    )
    _calendar()
    _records()


seeds.register("sparse", seeds.Seed(build_estate))


def _tailnet():
    """HQ's machine as a tailnet node, and the device the connection page is asked from."""

    now = timezone.now()
    ProviderInventory.objects.update_or_create(
        kind="tailscale.device",
        defaults={
            "observed_at": now,
            "records": [
                {
                    "name": MACHINE,
                    "addresses": [TAILNET_ADDRESS],
                    "dns_name": f"{MACHINE}.example.ts.net.",
                    "os": "linux",
                    "self": True,
                    "online": True,
                    "public_key": "test-key-hq",
                    "endpoints": [f"{ADDRESS}:41641"],
                },
                {
                    "name": "example-laptop",
                    # An IPv6 address too: the longest value an address row holds.
                    "addresses": [CALLER, "fd7a:115c:a1e0:ab12:4843:cd96:6258:b240"],
                    "dns_name": "example-laptop.example.ts.net.",
                    "os": "macOS",
                    "online": True,
                    "user": "example@example.com",
                    "public_key": "test-key-laptop",
                    "direct_endpoint": "198.51.100.7:41641",
                    "last_handshake": (now - timedelta(minutes=2)).isoformat(),
                    "active": True,
                    "rx_bytes": 123456,
                    "tx_bytes": 654321,
                    "endpoints": ["198.51.100.7:41641", "192.0.2.77:41641"],
                },
            ],
        },
    )


@contextmanager
def _asked_through_the_proxy():
    """The connection page as asked from the laptop, through HQ's proxy host."""

    with ExitStack() as stack:
        stack.enter_context(
            override_settings(
                ALLOWED_HOSTS=[HQ, "testserver"],
                SEVERINO_SITE_HOST=HQ,
                SEVERINO_TRUSTED_PROXIES=["172.18.0.0/16"],
            )
        )
        stack.enter_context(
            mock.patch("hq.platform.application.hq_self.host_addresses", return_value=frozenset({ADDRESS}))
        )
        yield


def _highlights():
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
    return {
        "highlights": [
            {
                "id": "example.first",
                "label": "Example one",
                "cards": [],
                "overview": DomainOverview("Example", "/example/", short),
            },
            {
                "id": "example.second",
                "label": "Example two",
                "cards": [],
                "overview": DomainOverview("Example", "/example/", long, (chart,), (calendar,)),
            },
        ],
        "compact": [{"label": "Example count", "value": 3, "url": "/example/"}],
    }


_LINKS = [{"href": "https://example.com/", "label": f"Example link {i}", "sub": "example.com"} for i in range(24)]


@contextmanager
def _dashboard(populated):
    highlights = _highlights() if populated else {"highlights": [], "compact": []}
    with ExitStack() as stack:
        stack.enter_context(
            mock.patch("hq.platform.core.dashboard_views.dashboard_highlights", return_value=highlights)
        )
        stack.enter_context(
            mock.patch(
                "hq.platform.core.dashboard_views.outward_links",
                return_value=(_LINKS if populated else [], None),
            )
        )
        if not populated:
            # Nothing contributes, the calendar's sources included.
            stack.enter_context(mock.patch("hq.platform.application.calendar.calendar_sources", return_value=()))
        else:
            # The machine an owner shows in the header, so its reading lines up
            # with the connection's and the controller's.
            from hq.domains.control_plane.models import DashboardMachine

            machine = ManagedResource.objects.filter(kind="machine").order_by("key").first()
            if machine is not None:
                DashboardMachine.objects.get_or_create(machine=machine)
        yield
        if populated:
            from hq.domains.control_plane.models import DashboardMachine

            DashboardMachine.objects.all().delete()


# The connection page's request: the laptop, forwarded by the proxy host.
_THROUGH_THE_PROXY = {
    "secure": True,
    "HTTP_HOST": HQ,
    "REMOTE_ADDR": PROXY,
    "HTTP_X_FORWARDED_FOR": CALLER,
    "HTTP_X_REAL_IP": CALLER,
    "HTTP_X_FORWARDED_PROTO": "https",
    "HTTP_X_FORWARDED_SCHEME": "https",
    # A header value as long as a browser's own.
    "HTTP_USER_AGENT": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
    ),
}


def _calendar():
    """A full month: a trip over a weekend, a timed visit with a
    long name and a place, a day too busy for its cell, a weekly class, and
    a long title in the last column, beside the month's edge."""

    from hq.domains.calendars.models import Entry

    today = timezone.localdate()
    sunday = today - timedelta(days=(today.weekday() + 1) % 7)
    Entry.objects.create(
        title="Example trip to a city with a long name",
        starts_on=sunday - timedelta(days=2),
        ends_on=sunday + timedelta(days=1),
    )
    Entry.objects.create(
        title="Example appointment with a long descriptive title",
        starts_on=today,
        starts_at=time(15),
        ends_at=time(16),
        location="An example clinic on a long street name",
    )
    for index in range(5):
        Entry.objects.create(title=f"Example errand {index}", starts_on=today + timedelta(days=1))
    Entry.objects.create(title="Example class", starts_on=today, repeat="weekly", weekdays="0,2", starts_at=time(18))
    # Every Saturday the month shows, so one holds it beside the edge however
    # busy the day after today is.
    Entry.objects.create(
        title="Example review of the self-hosted-git mirrors",
        starts_on=sunday - timedelta(weeks=6),
        repeat="weekly",
        weekdays="5",
        starts_at=time(9),
    )


ASSET = "example-server"


def _records():
    """Records that name each other: an asset that is the machine, with an
    event and an expense about it, and a ledger in every category."""

    from decimal import Decimal

    from hq.domains.assets.models import Asset
    from hq.domains.calendars.models import Entry
    from hq.domains.expenses.models import EXPENSE_CATEGORY_CHOICES, Expense
    from hq.domains.receipts.models import Receipt

    machine = f"machine:{MACHINE}"
    asset = Asset.objects.create(
        item_name="Example server with a long descriptive product name",
        slug=ASSET,
        purchase_date=date(2025, 1, 5),
        total_cost=Decimal("1234.50"),
        infrastructure=machine,
        infrastructure_name=MACHINE,
    )
    today = timezone.localdate()
    Entry.objects.create(
        title="Example yearly service with a long descriptive title",
        starts_on=today + timedelta(days=3),
        repeat="yearly",
        about=f"asset:{asset.slug}",
        about_name=asset.item_name,
    )
    for index, (category, _label) in enumerate(EXPENSE_CATEGORY_CHOICES):
        Expense.objects.create(
            date=today - timedelta(days=index),
            vendor=f"Example vendor {index}",
            item="An example purchase with a long description",
            category=category,
            total_cost=Decimal("1234.50") * (index + 1),
            related_asset=asset if index == 0 else None,
            about=machine if index < 2 else "",
            about_name=MACHINE if index < 2 else "",
        )
    # A receipt no expense claims yet, whose vendor names every one of them.
    Receipt.objects.create(file="receipts/example.pdf", original_filename="example.pdf", vendor="Example vendor")


def _expense() -> str:
    from hq.domains.expenses.models import Expense

    return Expense.objects.exclude(about="").first().get_absolute_url()


def _receipt_match() -> str:
    from hq.domains.receipts.models import Receipt

    return reverse("receipts:match", args=[Receipt.objects.get().pk])


def _calendar_day() -> str:
    from hq.domains.calendars.models import Entry

    return Entry.objects.get(starts_at=time(15)).get_absolute_url()


# name -> (url, context manager for what no view reads from the database[,
# the request's own fields])
_MESSAGE = {
    "connection_ref": "example-cloudflare",
    "account_id": "example-account",
    "database_id": "example-database",
    "database": "contacts",
    "id": 1,
    # The widest moment a list writes: another year's, at a two-digit hour.
    "created_at": "2025-12-28 17:58:00",
    "updated_at": "2025-12-28 17:58:00",
    "name": "Example Person",
    "email": "bartholomew.featherstonehaugh@example.com",
    "message": "Hello from the contact form.",
    "status": "unread",
    "country": "US",
    "turnstile": "verified",
}


@contextmanager
def _messages():
    """The inbox with a message in it, as the controller's reading holds it."""

    from hq.domains.contacts import inbox

    ProviderInventory.objects.update_or_create(
        kind=inbox.READING, defaults={"records": [_MESSAGE], "observed_at": timezone.now(), "reachable": True}
    )
    yield


PAGES = {
    "dashboard": (lambda: reverse("dashboard"), lambda: _dashboard(True)),
    "dashboard-bare": (lambda: reverse("dashboard"), lambda: _dashboard(False)),
    "service": (
        lambda: reverse("control_plane:service", kwargs={"hostname": SERVICE}),
        ExitStack,
    ),
    "connections": (lambda: reverse("control_plane:connections"), ExitStack),
    "machine": (
        lambda: reverse("control_plane:machine", kwargs={"name": MACHINE}),
        ExitStack,
    ),
    "topology": (lambda: reverse("control_plane:topology"), ExitStack),
    "services": (lambda: reverse("control_plane:services"), ExitStack),
    "findings": (lambda: reverse("control_plane:findings"), ExitStack),
    "containers": (lambda: reverse("control_plane:containers"), ExitStack),
    "resource": (lambda: reverse("control_plane:detail", kwargs={"key": "hq-proxy"}), ExitStack),
    "action-items": (lambda: reverse("action_items"), ExitStack),
    "projects": (lambda: reverse("projects:list"), ExitStack),
    # A command that changes infrastructure, so the page carries its consent
    # checkbox, which must not stretch across the row like a form field and
    # push its label out past the edge.
    "command-consent": (
        lambda: reverse("command", kwargs={"name": "infrastructure.controller.refresh"}),
        ExitStack,
    ),
    # A command that writes a record: selects, dates and fields side by side,
    # and a textarea taking its row.
    "command-form": (lambda: reverse("command", kwargs={"name": "asset.create"}), ExitStack),
    "connection": (lambda: reverse("connection"), _asked_through_the_proxy, _THROUGH_THE_PROXY),
    # A band whose cells are cards: the frame rule must not zero their padding.
    "resource-kinds": (lambda: reverse("control_plane:create"), ExitStack),
    # A form laid out as a grid of fields, with a textarea taking the row.
    "project-form": (lambda: reverse("projects:create"), ExitStack),
    "calendar": (lambda: reverse("calendar:month"), ExitStack),
    "messages": (lambda: reverse("contacts:list"), _messages),
    # A day open beside the month, with an entry open in it.
    "calendar-day": (_calendar_day, ExitStack),
    # Records that name each other, and the form that picks what one names.
    "asset": (lambda: reverse("assets:detail", args=[ASSET]), ExitStack),
    "expense": (_expense, ExitStack),
    "event-form": (lambda: reverse("calendar:entry_new"), ExitStack),
    "receipt-match": (_receipt_match, ExitStack),
}


def render_pages(user, pages=None):
    """Every page in ``pages`` (PAGES by default) as its view renders it for ``user``."""

    client = Client()
    client.force_login(user)
    rendered = {}
    for name, (url, patches, *request) in (PAGES if pages is None else pages).items():
        with patches():
            response = client.get(url(), **(request[0] if request else {}))
        if response.status_code != 200:
            raise AssertionError(f"{name} answered {response.status_code}")
        rendered[name] = answered(response)
    return rendered

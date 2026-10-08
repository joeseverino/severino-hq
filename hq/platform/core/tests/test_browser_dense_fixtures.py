"""A dense synthetic estate for the browser layout gate: production-shaped data.

The sparse estate in core/tests/test_browser_fixtures.py proves a page lays out. This
one proves it still does with what an operator's estate actually holds: names
of 40 and more characters, hyphenated so a browser may break them only at a
hyphen, one machine serving a dozen hostnames and running twenty containers, a
two-word weather condition beside its temperature, a request path with every
kind of hop, many findings, and long project names.

Each of these can lay out wrong while the sparse gate passes: a weather
condition drawn under the glance controls, a seven-hop path
wrapping names mid-word and clipping its last hop, a machine row as tall as the
hostnames it serves. Every name is example.*; every address is a documentation
or shared-address range (192.0.2.0/24, 198.51.100.0/24, 100.64.0.0/10).
"""

from contextlib import ExitStack
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import (
    DashboardConfiguration,
    DashboardMachine,
    ManagedResource,
    ProviderInventory,
    WeatherObservation,
)
from hq.domains.projects.models import Project
from hq.platform.application import readings
from hq.platform.application.pins import SERVICE as PINNED_SERVICE, toggle
from hq.platform.application.report_testing import report_connections
from hq.platform.application.security import cli_principal
from hq.platform.application.services import CONTAINER_KIND
from hq.platform.core.models import AuditLog
from hq.platform.core.tests.test_browser_fixtures import (
    _THROUGH_THE_PROXY,
    CALLER,
    HQ,
    _asked_through_the_proxy,
    _dashboard,
    _healthy,
    render_pages,
)

# Hyphenated on purpose: a browser breaks a name at a hyphen, and a name that
# wraps there mid-word is the failure this estate exists to show.
MACHINE = "example-primary-container-host-with-a-long-name"
EDGE_MACHINE = "example-secondary-edge-host-with-a-long-name"
ADDRESS = "192.0.2.10"
EDGE_ADDRESS = "198.51.100.20"
TAILNET_ADDRESS = "100.64.0.10"
EDGE_TAILNET_ADDRESS = "100.64.0.11"
# A name answered by an alias, answered by a tailnet address, served by a proxy
# on one machine, forwarded to a container: every kind of hop the strip draws.
SERVICE = "a-long-hyphenated-service-name-for-the-layout-gate.example.com"
ALIAS = "an-internal-alias-with-a-deliberately-long-name.example.com"
FRONTEND = "example-web-frontend-with-a-deliberately-long-name"
HOSTNAMES = 12
CONTAINERS = 20
FINDINGS = 10
PORTAINER = "an-example-portainer-connection-with-a-long-name"


def _hostname(index):
    return f"example-hostname-with-a-long-descriptive-name-{index:02d}.example.com"


def _container(index):
    return f"example-background-worker-with-a-long-name-{index:02d}"


def _proxy(key, domain, forward_host, port):
    return _healthy(
        key,
        "npm.proxy_host",
        {
            "domain_names": [domain],
            "forward_scheme": "http",
            "forward_host": forward_host,
            "forward_port": port,
        },
    )


def _machines():
    ManagedResource.objects.create(
        key="example-primary-host",
        kind="machine",
        spec={
            "name": MACHINE,
            "role": "Docker host for every example workload, with a role long enough to wrap",
            "addresses": [ADDRESS, TAILNET_ADDRESS],
        },
    )
    ManagedResource.objects.create(
        key="example-edge-host",
        kind="machine",
        spec={
            "name": EDGE_MACHINE,
            "role": "Reverse proxy",
            "addresses": [EDGE_ADDRESS, EDGE_TAILNET_ADDRESS],
        },
    )
    # Declared and on no network HQ reads: its state is two words.
    ManagedResource.objects.create(
        key="example-printer",
        kind="machine",
        spec={
            "name": "example-printer-with-a-long-name",
            "role": "Network printer",
            "addresses": ["192.0.2.77"],
        },
    )
    now = timezone.now()
    ProviderInventory.objects.update_or_create(
        kind="tailscale.device",
        defaults={
            "observed_at": now,
            "records": [
                {
                    "name": name,
                    "addresses": [address],
                    "dns_name": f"{name}.example.ts.net.",
                    "os": "linux",
                    "self": name == MACHINE,
                    "online": True,
                    "public_key": f"test-key-{index}",
                    "endpoints": [f"{public}:41641"],
                }
                for index, (name, address, public) in enumerate(
                    (
                        (MACHINE, TAILNET_ADDRESS, ADDRESS),
                        (EDGE_MACHINE, EDGE_TAILNET_ADDRESS, EDGE_ADDRESS),
                    )
                )
            ]
            + [
                {
                    "name": "example-operators-laptop-with-a-long-name",
                    "addresses": [CALLER],
                    "dns_name": "example-operators-laptop-with-a-long-name.example.ts.net.",
                    "os": "macOS",
                    "online": True,
                    "user": "example@example.com",
                    "public_key": "test-key-laptop",
                    "direct_endpoint": "198.51.100.7:41641",
                    "last_handshake": (now - timedelta(minutes=2)).isoformat(),
                    "active": True,
                    "endpoints": ["198.51.100.7:41641"],
                }
            ],
        },
    )


def _routes():
    """The seven-hop service, HQ's own name, and a dozen names on one machine."""

    _healthy("dense-service-dns", "adguard.rewrite", {"domain": SERVICE, "answer": ALIAS})
    _healthy("dense-alias-dns", "adguard.rewrite", {"domain": ALIAS, "answer": TAILNET_ADDRESS})
    _proxy("dense-service-proxy", SERVICE, FRONTEND, 8080)
    _proxy("dense-alias-proxy", ALIAS, FRONTEND, 8080)
    _healthy("dense-hq-dns", "adguard.rewrite", {"domain": HQ, "answer": TAILNET_ADDRESS})
    _proxy("dense-hq-proxy", HQ, ADDRESS, 8000)
    for index in range(HOSTNAMES):
        _proxy(f"dense-host-{index:02d}", _hostname(index), ADDRESS, 9000 + index)
        _healthy(
            f"dense-host-dns-{index:02d}",
            "adguard.rewrite",
            {"domain": _hostname(index), "answer": TAILNET_ADDRESS},
        )
    _healthy(
        "example-wildcard",
        "tls.certificate",
        {
            "certificate_name": "example-wildcard-certificate-with-a-long-name",
            "domains": ["example.com", "*.example.com"],
            "install_on": ["dense-service-proxy"],
            "renewal_window_days": 30,
        },
    )


def _findings():
    """Records a sweep skipped: one finding each, with evidence to lay out."""

    now = timezone.now()
    for index in range(FINDINGS + 3):
        record = ManagedResource.objects.create(
            key=f"dense-skipped-{index:02d}",
            kind="adguard.rewrite",
            spec={
                "domain": f"a-rewrite-a-sweep-skipped-with-a-long-name-{index:02d}.example.com",
                "answer": ADDRESS,
            },
        )
        # The first three were swept now; the rest fell six hours behind them.
        when = now if index < 3 else now - timedelta(hours=6)
        ManagedResource.objects.filter(pk=record.pk).update(
            last_observed_at=when,
            generation=1,
            observed_generation=1,
            status=dict(record.spec),
            conditions=[{"type": "Ready", "status": True, "reason": "Reconciled", "message": ""}],
        )


def _containers():
    records = [
        {
            "name": FRONTEND,
            "host": MACHINE,
            "stack": FRONTEND,
            "image": "registry.example.com/example/a-web-frontend-image-with-a-long-name:2026.09.28",
            "state": "running",
            "ports": [8080],
            "connection_ref": PORTAINER,
        }
    ] + [
        {
            "name": _container(index),
            "host": MACHINE,
            "image": f"registry.example.com/example/a-worker-image-with-a-long-name-{index:02d}:1.2.3",
            "state": "running" if index % 4 else "exited",
            "ports": [9000 + index],
        }
        for index in range(CONTAINERS - 1)
    ]
    ProviderInventory.objects.update_or_create(
        kind=CONTAINER_KIND,
        defaults={"records": records, "reachable": True, "observed_at": timezone.now()},
    )
    _hardening()


def _hardening():
    """The frontend, watched, failing every check a compose file can meet: its
    page carries the whole compose change, and the action items a button each."""

    ManagedResource.objects.create(
        key="dense-frontend",
        kind=CONTAINER_KIND,
        spec={"connection_ref": PORTAINER, "host": MACHINE, "name": FRONTEND},
    )
    source = "/opt/example-applications/a-web-frontend-with-a-long-name/configuration"
    for kind, record in (
        (
            "portainer.runtime",
            {
                "connection_ref": PORTAINER,
                "host": MACHINE,
                "container": FRONTEND,
                "service": "a-web-frontend-service-with-a-deliberately-long-name",
                "privileged": True,
                "pid_mode": "host",
                "cap_add": ["SYS_ADMIN", "NET_BIND_SERVICE"],
                "security_opt": ["seccomp=unconfined"],
                "port_bindings": [{"container_port": "8080/tcp", "host_ip": "0.0.0.0", "host_port": "8080"}],
                "mounts": [
                    {
                        "type": "bind",
                        "source": "/var/run/docker.sock",
                        "destination": "/var/run/docker.sock",
                        "read_only": True,
                    },
                    {
                        "type": "bind",
                        "source": "/etc/example-configuration-directory",
                        "destination": "/host-etc",
                        "read_only": False,
                    },
                    {"type": "bind", "source": source, "destination": "/srv/configuration", "read_only": False},
                ],
            },
        ),
        (
            "portainer.compose_project",
            {
                "connection_ref": PORTAINER,
                "host": MACHINE,
                "name": FRONTEND,
                "config_files": [
                    "/opt/example-applications/a-web-frontend-with-a-long-name/docker-compose.production.yml"
                ],
            },
        ),
    ):
        ProviderInventory.objects.update_or_create(
            kind=kind,
            defaults={"records": [record], "reachable": True, "observed_at": timezone.now()},
        )


def _glance():
    """Both machines and the weather on the dashboard, every panel read."""

    now = timezone.now()
    DashboardConfiguration.objects.update_or_create(
        pk=1,
        defaults={"weather_point": "41.0000,-87.0000", "weather_label": "Weather"},
    )
    for position, key in enumerate(("example-primary-host", "example-edge-host")):
        DashboardMachine.objects.create(machine=ManagedResource.objects.get(key=key), position=position)
        readings.record(
            readings.machine_telemetry(key),
            {
                "status": "good",
                "summary": "Host load 0.42",
                "metrics": [
                    {"label": "Container CPU", "value": "37%", "detail": ""},
                    {"label": "Container memory", "value": "81%", "detail": ""},
                    {"label": "Docker storage", "value": "64%", "detail": ""},
                ],
            },
            observed_at=now,
        )
    WeatherObservation.objects.create(
        point="41.0000,-87.0000",
        payload={
            "status": "serious",
            "summary": "Example City, EX",
            "metrics": [
                {"label": "Now", "value": "Partly Sunny", "detail": "This Afternoon"},
                {"label": "Temperature", "value": "73°F", "detail": ""},
                {"label": "Wind", "value": "SW 10 mph", "detail": "NWS hourly forecast"},
                {"label": "Alerts", "value": "2", "detail": "active for this point"},
            ],
        },
        observed_at=now,
    )


def build_dense_estate():
    """The dense estate, and the user its pages are rendered for."""

    user = get_user_model().objects.create_user(
        username="example-dense", password="not-a-real-password", first_name="Example"
    )
    for index in range(8):
        Project.objects.create(
            name=f"An-example-project-with-a-deliberately-long-hyphenated-name-{index}",
            slug=f"an-example-project-with-a-long-name-{index}",
            category="homelab",
            status="active",
            public_url=f"https://{_hostname(index)}/",
            repository_url=f"https://github.com/example/an-example-repository-with-a-long-name-{index}",
        )
    _machines()
    _routes()
    # Some services are favorites and some are not, so the list has sections.
    for hostname in (SERVICE, _hostname(0)):
        toggle(user, PINNED_SERVICE, hostname)
    _findings()
    _containers()
    _glance()
    report_connections(
        [
            {
                "connection_ref": PORTAINER,
                "provider": "portainer",
                "endpoint": "https://a-portainer-endpoint-with-a-long-name.example.com",
                "reaches": [MACHINE, EDGE_MACHINE],
                "ok": True,
                "probed": True,
                "detail": "2 of 2 environments reachable.",
            },
            {
                "connection_ref": "an-example-proxy-connection-with-a-long-name",
                "provider": "npm",
                # As long as a real API path runs: an account, a resource, an id.
                "endpoint": (
                    "https://a-proxy-manager-endpoint-with-a-long-name.example.com/client/v4/accounts/"
                    "0123456789abcdef0123456789abcdef/databases/01234567-89ab-cdef-0123-456789abcdef"
                ),
                "reaches": [EDGE_MACHINE],
                "ok": True,
                "probed": True,
                "detail": f"{HOSTNAMES + 4} proxy hosts.",
            },
            {
                # On a catalogued machine: the row says which, as a link in
                # the smaller line under the endpoint.
                "connection_ref": "an-example-dns-server-connection",
                "provider": "adguard",
                "endpoint": f"http://{ADDRESS}:3001",
                "reaches": [MACHINE],
                "ok": True,
                "probed": True,
                "detail": "1 server.",
            },
            {
                # Several abilities beside a crowded Depends cell: the row that
                # squeezes the abilities column until its words break.
                "connection_ref": "an-example-dns-connection-with-a-long-name",
                "provider": "cloudflare_dns",
                "endpoint": "https://api.example.com/client/v4/accounts/0123456789abcdef0123456789abcdef",
                "reaches": [f"an-example-zone-{index}.example.com" for index in range(4)],
                "ok": True,
                "probed": True,
                "detail": "4 zones.",
            },
        ],
        principal=cli_principal(),
        controller_id="example-controller",
    )
    # The last thing a connection did names a resource as long as a real
    # container's: a stack prefix, a service and a replica.
    AuditLog.objects.create(
        action=AuditLog.Action.UPDATED,
        object_type="container",
        object_repr="an-example-edge-machine-agent-stack-an-example-agent-service-1",
        connection=PORTAINER,
    )
    return user


# The same kind of table as test_browser_fixtures.PAGES, under dense/ so a
# failure names which estate it came from.
DENSE_PAGES = {
    "dense/dashboard": (lambda: reverse("dashboard"), lambda: _dashboard(True)),
    "dense/service": (
        lambda: reverse("control_plane:service", kwargs={"hostname": SERVICE}),
        ExitStack,
    ),
    "dense/machines": (lambda: reverse("control_plane:machines"), ExitStack),
    "dense/machine": (
        lambda: reverse("control_plane:machine", kwargs={"name": MACHINE}),
        ExitStack,
    ),
    "dense/containers": (lambda: reverse("control_plane:containers"), ExitStack),
    "dense/container": (
        lambda: reverse("control_plane:detail", kwargs={"key": "dense-frontend"}),
        ExitStack,
    ),
    "dense/services": (lambda: reverse("control_plane:services"), ExitStack),
    "dense/findings": (lambda: reverse("control_plane:findings"), ExitStack),
    "dense/action-items": (lambda: reverse("action_items"), ExitStack),
    "dense/projects": (lambda: reverse("projects:list"), ExitStack),
    "dense/connections": (
        lambda: reverse("control_plane:connections"),
        _asked_through_the_proxy,
        _THROUGH_THE_PROXY,
    ),
}


def render_dense_pages(user):
    return render_pages(user, DENSE_PAGES)

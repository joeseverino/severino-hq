"""The machine, every name it serves, the certificate in front of them, and
the connections HQ reads them through."""

from datetime import timedelta

from django.utils.timezone import localtime

from hq.domains.control_plane.models import (
    DashboardConfiguration,
    DashboardMachine,
    DashboardRefreshRequest,
    ManagedResource,
    ProviderInventory,
)
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.observations.glance import MACHINE_KIND as MACHINE_TELEMETRY, WEATHER_KIND
from hq.domains.control_plane.observations.r2 import BACKUP_KIND
from hq.domains.control_plane.observations.tailscale import EXIT_ROUTES
from hq.platform.application import cadence
from hq.platform.application.conditions import READY
from hq.platform.application.inventory import keep
from hq.platform.application.outbound_work import reader_of
from hq.platform.application.report_testing import report_connections

from . import doors, network, work
from .names import (
    ADGUARD,
    BACKUPS,
    CLOUDFLARE,
    CONTAINERS,
    CONTROLLER,
    DNS,
    EDGE,
    GITHUB,
    HQ,
    LAN,
    LAPTOP,
    LAPTOP_NAME,
    MACHINE,
    PORTAINER,
    PROXY,
    PROXY_REF,
    PUBLIC,
    SERVICES,
    SSH,
    TAILNET,
    TAILNET6,
    TAILSCALE,
    ZONE,
    container_id,
    iso,
    magic_dns,
    node_id,
    now,
    stamp,
    tailnet_v4,
    tailnet_v6,
)
from .sweep import store

CERTIFICATE = "wildcard-example-com"
# Inside its renewal window, so renewing it is one of the things to do.
CERTIFICATE_EXPIRES = timedelta(days=24)
# Left in the proxy from before the wildcard, when the media server answered
# at this name: one certificate twice, as the proxy issued it and as it was
# uploaded by hand, lapsed on one day and serving nothing, so deleting them
# is one of the things to do.
LEFT_OVER = "media.example.com"
LEFT_OVER_LAPSED = timedelta(days=12)


def declare():
    ManagedResource.objects.create(
        key=MACHINE,
        kind="machine",
        spec={"name": MACHINE, "role": "Docker host", "addresses": [LAN, TAILNET, TAILNET6]},
    )
    network.declare_rewrites()
    for hostname, container, port, _image in SERVICES:
        ManagedResource.objects.create(key=f"{container}-proxy", kind="npm.proxy_host", spec=_route(hostname, port))
    for container, _port, _image in CONTAINERS:
        spec = {"connection_ref": PORTAINER, "host": MACHINE, "name": container}
        if container == "portainer":
            spec["holds_docker_socket"] = True
        ManagedResource.objects.create(key=f"{container}-container", kind="portainer.container", spec=spec)
    ManagedResource.objects.create(
        key=CERTIFICATE,
        kind="tls.certificate",
        spec={
            "certificate_name": CERTIFICATE,
            "domains": [f"*.{ZONE}"],
            "install_on": [PROXY_REF],
            "renewal_window_days": 30,
        },
    )
    ManagedResource.objects.create(
        key=f"{PROXY_REF}-certificate-target",
        kind="tls.delivery_target",
        spec={
            "kind": "npm",
            "connection_ref": PROXY_REF,
            "name": CERTIFICATE,
            "certificate_resource": CERTIFICATE,
            "verify_domains": [HQ],
        },
    )
    network.declare_zone()
    _observe_everything()


def _route(hostname, port):
    return {"domain_names": [hostname], "forward_scheme": "http", "forward_host": LAN, "forward_port": port}


def _observe_everything():
    """One sweep read every declaration just now, and found it as declared.

    The certificate also carries what its last issue reported."""

    issued = {
        CERTIFICATE: {
            "issuer": "Let's Encrypt",
            "not_after": (now() + CERTIFICATE_EXPIRES).isoformat(),
            "not_before": (now() + CERTIFICATE_EXPIRES - timedelta(days=90)).isoformat(),
        }
    }
    for record in ManagedResource.objects.all():
        ManagedResource.objects.filter(pk=record.pk).update(
            last_observed_at=now(),
            generation=1,
            observed_generation=1,
            status={**record.spec, **issued.get(record.key, {})},
            conditions=[{"type": READY, "status": True, "reason": "Reconciled", "message": ""}],
        )


def _answering(ref, provider, endpoint, detail, reaches=()):
    # Each manages, as a set-up HQ's do, so every action is offered. Only a
    # controller applies one, and the estate has none.
    return {
        "connection_ref": ref,
        "provider": provider,
        "endpoint": endpoint,
        "manages": True,
        "reaches": list(reaches),
        "ok": True,
        "probed": True,
        "detail": detail,
    }


def connections(*more):
    """Each connection the controller reports, and ``more`` beside them."""

    answering = _answering
    # Bound as the bridge binds a controller's report, so its rows name the controller.
    with doors.controller("connections"):
        report_connections(
            [
                answering(
                    PORTAINER, "portainer", "https://portainer.example.com", "1 of 1 environment reachable.", [MACHINE]
                ),
                answering(DNS, "cloudflare_dns", "https://api.cloudflare.com/client/v4", "1 zone.", [ZONE]),
                answering(CLOUDFLARE, "cloudflare_api", "https://api.cloudflare.com/client/v4", "1 account.", [ZONE]),
                answering(PROXY_REF, "npm", f"http://{LAN}:81", f"{len(SERVICES)} proxy hosts."),
                answering(ADGUARD, "adguard", f"http://{LAN}:3080", f"{len(SERVICES) + 1} rewrites."),
                answering(TAILSCALE, "tailscale", "https://api.tailscale.com", "3 devices."),
                answering(SSH, "ssh", f"{MACHINE}:22", "Host key matches.", [MACHINE]),
                # The accounts the app is installed on, as the controller's probe reports them.
                answering(
                    GITHUB,
                    "github_app",
                    "https://api.github.com",
                    f"{len(work.REPOSITORIES)} repositories.",
                    ["example"],
                ),
                answering("onepassword", "onepassword", "https://my.1password.com", "1 vault."),
                answering(BACKUPS, "r2", "https://backups.example.com", "Bucket example-backups."),
                *more,
            ],
            controller_id=CONTROLLER,
        )


# A machine HQ knows that the playbook worker has no login to, and a shell
# into a web host's account, whose sweep reads no Caddy route there.
PRINTER = "office-printer"
WEB_HOST = "web-host"


def unreached():
    """A printer on the LAN the playbook worker has no login to, so no
    playbook is offered or said to run there; and two shell connections, the
    machine's serving a Caddy route and the web host's serving none, so each
    is said to change only what it does."""

    ManagedResource.objects.create(
        key=PRINTER, kind="machine", spec={"name": PRINTER, "role": "Printer", "addresses": ["10.100.200.50"]}
    )
    connections(_answering(WEB_HOST, "ssh", "203.0.113.20:22", "Host key matches.", [WEB_HOST]))
    store(
        "caddy.route",
        {"connection_ref": SSH, "domain": f"status.{ZONE}", "upstream": "localhost:3001", "to_requested_host": False},
    )


# What the edge machine runs, and the names its Caddy hands to it or answers itself.
EDGE_CONTAINER = "uptime-kuma"
EDGE_NAMES = (f"uptime.{ZONE}", f"health.{ZONE}")


def edge():
    """An edge machine on the tailnet, approved as an exit node, whose Caddy
    hands ``uptime`` to a container by its name and answers ``health``
    itself, both declared: its page, its container's and the topology place
    both names on it, from the path each name's request takes."""

    address = tailnet_v4(EDGE)
    connections(_answering(EDGE, "ssh", f"{address}:22", "Host key matches.", [EDGE]))
    uptime, health = EDGE_NAMES
    routes = {uptime: f"{EDGE_CONTAINER}:3001", health: ""}
    for name, upstream in routes.items():
        ManagedResource.objects.create(
            key=f"{name.split('.')[0]}-caddy",
            kind="caddy.route",
            spec={"connection_ref": EDGE, "domain": name, "upstream": upstream},
        )
    ManagedResource.objects.create(
        key=f"{EDGE_CONTAINER}-container",
        kind="portainer.container",
        spec={"connection_ref": PORTAINER, "host": EDGE, "name": EDGE_CONTAINER},
    )
    store(
        "caddy.route",
        *(
            {"connection_ref": EDGE, "domain": name, "upstream": upstream, "to_requested_host": False}
            for name, upstream in routes.items()
        ),
    )
    _appended(
        "tailscale.device",
        {
            "name": EDGE,
            "node_id": node_id(EDGE),
            "addresses": [address, tailnet_v6(EDGE)],
            "dns_name": magic_dns(EDGE),
            "os": "linux",
            "online": True,
            "tags": ["tag:server"],
            "advertised_routes": sorted(EXIT_ROUTES),
            "enabled_routes": sorted(EXIT_ROUTES),
            "offers_exit_node": True,
            "exit_node_approved": True,
            "client_version": "1.76.6-t3b1a6e2c4-g5e7f9a1b3",
            "last_handshake": iso(timedelta(minutes=1)),
        },
    )
    _appended(
        "portainer.container",
        {
            "id": container_id(EDGE_CONTAINER),
            "name": EDGE_CONTAINER,
            "stack": EDGE_CONTAINER,
            "host": EDGE,
            "host_address": address,
            "connection_ref": PORTAINER,
            "image": "louislam/uptime-kuma:1.23.16",
            "state": "running",
            "status": "Up 3 days",
            "ports": [3001],
            "network_mode": "bridge",
            "portainer_managed": False,
        },
    )
    _appended("adguard.rewrite", *({"connection_ref": ADGUARD, "domain": name, "answer": address} for name in routes))


def _appended(kind, *records):
    """``records`` added to the reading of ``kind`` the estate already holds."""

    held = ProviderInventory.objects.get(kind=kind)
    held.records = [*held.records, *records]
    held.save(update_fields=["records"])


def proxy():
    """Nginx Proxy Manager: the routes, the certificate it serves and who may pass."""

    hosts = [hostname for hostname, *_rest in SERVICES]
    store(
        "npm.proxy_host",
        *(
            {**_route(h, p), "connection_ref": PROXY_REF, "access_list_id": 1, "access_policy": network.TAILNET_ONLY}
            for h, _c, p, _i in SERVICES
        ),
    )
    store(
        "npm.certificate",
        {
            "connection_ref": PROXY_REF,
            "id": 1,
            "name": CERTIFICATE,
            "provider": "other",
            "domains": [f"*.{ZONE}"],
            "expires_on": (now() + CERTIFICATE_EXPIRES).isoformat(),
            "serves": hosts,
        },
        *(
            {
                "connection_ref": PROXY_REF,
                "id": number,
                "name": LEFT_OVER,
                "provider": provider,
                "domains": [LEFT_OVER],
                "expires_on": (now() - LEFT_OVER_LAPSED).isoformat(),
                "serves": [],
            }
            for number, provider in ((2, "letsencrypt"), (3, "other"))
        ),
    )
    store(
        "npm.access_list",
        {
            "connection_ref": PROXY_REF,
            "id": 1,
            "name": "tailnet only",
            "satisfy_any": False,
            "pass_auth": False,
            "clients": network.TAILNET_ONLY["clients"],
            "logins": [],
            "protects": hosts,
        },
    )
    for kind in ("npm.redirect", "npm.stream", "npm.dead_host"):
        store(kind)


def host():
    """What the host reports of itself: its firewall, perimeter, disks, restart and load."""

    # A kernel and a library installed two days ago wait on a restart, which is
    # scheduled for a quiet hour. The controller runs on the edge, so its own
    # reading and the edge receiver's are of one machine, and say the same.
    restart = {
        "pending": True,
        "since": stamp(timedelta(days=2)),
        "packages": ["linux-image-generic", "libc6"],
        "scheduled_action": "reboot",
        "scheduled_at": stamp(-timedelta(hours=7)),
        "read_at": stamp(),
    }
    store("host.reboot", restart)
    store("host.edge_reboot", {"connection_ref": SSH, **restart})

    # A root a build has all but filled, beside a roomy boot partition.
    store(
        "host.filesystem",
        {"mount": "/", "free_bytes": 59_000_000, "total_bytes": 29 * 2**30, "read_at": stamp()},
        {"mount": "/boot", "free_bytes": 760 * 2**20, "total_bytes": 2**30, "read_at": stamp()},
    )
    store(
        "host.firewall",
        {
            "record": "interface-binding",
            "interface": "tailscale0",
            "accept_requires_interface": True,
            "foreign_interface_dropped": True,
            "read_at": stamp(),
        },
    )
    store(
        "host.perimeter",
        {
            "record": "perimeter",
            "connection_ref": SSH,
            "firewall_unit": "active",
            "public_addresses": [PUBLIC],
            "ports_checked": [22, 80, 443, 8000],
            "answered_publicly": [],
            "read_at": stamp(timedelta(minutes=9)),
        },
    )
    store(
        "registry.address",
        {
            "address": PUBLIC,
            "organization": "Prairie Fiber",
            "name": "PRAIRIE-RESIDENTIAL",
            "handle": "NET-198-51-100-0-1",
            "country": "US",
            "range": "198.51.100.0/24",
            "read_at": iso(timedelta(hours=3)),
        },
    )
    # Last night's archive, copied off-site by the pass after it, beside the
    # three months the bucket's lock keeps.
    archived = timedelta(hours=7)
    store(
        BACKUP_KIND,
        {
            "connection_ref": BACKUPS,
            "bucket": "example-backups",
            "key": f"severino-hq-{stamp(archived).replace('-', '').replace(':', '')}.tar.gz.age",
            "size_bytes": 48_213_904,
            "uploaded_at": stamp(archived - timedelta(minutes=12)),
            "copies": 90,
        },
    )
    store("caddy.route")
    # Every request this week came from the laptop, through the proxy.
    store(
        "hq.request_path",
        {
            "address": LAPTOP,
            "device": LAPTOP_NAME,
            "channel": "tailnet",
            "carried": "direct",
            "forwarded_by": PROXY,
            "first_seen": iso(timedelta(days=120)),
            "last_seen": iso(),
            "count": 1_284,
            "window_start": iso(timedelta(days=7)),
        },
    )
    DashboardMachine.objects.create(machine=ManagedResource.objects.get(key=MACHINE), position=0)
    # The machine read through its Docker environment, as the controller
    # reports it: what its running containers take of it.
    _telemetry(now())
    _weather()


def _telemetry(read_at):
    keep(
        MACHINE_TELEMETRY,
        "key",
        {
            "key": MACHINE,
            "docker": {
                "cpu_percent": 12.0,
                "cores": 4,
                "memory_used": 4_123_000_000,
                "memory_total": 8_589_934_592,
                "storage": 41_000_000_000,
            },
        },
        read_at=read_at,
    )


# The hours ahead, as the weather service lists its periods: a showery
# afternoon that clears by evening.
_HOURS = (
    (58, 40, "Chance Light Rain Showers"),
    (59, 40, "Chance Light Rain Showers"),
    (60, 30, "Chance Light Rain Showers"),
    (60, 20, "Slight Chance Light Rain"),
    (58, 10, "Partly Cloudy"),
    (56, 5, "Partly Cloudy"),
    (54, 2, "Mostly Clear"),
    (53, 1, "Mostly Clear"),
    (52, 1, "Clear"),
    (51, 0, "Clear"),
    (50, 0, "Clear"),
    (49, 0, "Clear"),
)


_POINT = "40.0000,-90.0000"


def _weather():
    """The dashboard's forecast, in the shape the controller's weather
    reading reports one."""

    point = _POINT
    DashboardConfiguration.objects.update_or_create(
        pk=1, defaults={"weather_point": point, "weather_label": "Fairview"}
    )
    observed = now() - timedelta(minutes=14)
    first = localtime(observed).replace(minute=0, second=0, microsecond=0)
    hours = [
        {
            "start": (first + timedelta(hours=index)).isoformat(),
            "temperature": degrees,
            "temperature_unit": "F",
            "forecast": forecast,
            "chance": chance,
            "wind_speed": "10 to 15 mph",
            "wind_direction": "WSW",
        }
        for index, (degrees, chance, forecast) in enumerate(_HOURS)
    ]
    hours[0]["name"] = "This Afternoon"
    keep(WEATHER_KIND, "point", {"point": point, "city": "Fairview", "state": "IL", "hours": hours}, read_at=observed)


def glance_aged():
    """The machine's reading 22 minutes old, as the dashboard finds it when
    nobody has looked since: due again, and no fault."""

    _telemetry(now() - timedelta(minutes=22))


def glance_asked():
    """The machine's reading a day old and the forecast never read, each
    with a refresh the dashboard asked for itself still under way."""

    _telemetry(now() - timedelta(days=1))
    ProviderInventory.objects.filter(kind=WEATHER_KIND).delete()
    machine = ManagedResource.objects.get(key=MACHINE)
    cadence.ask(panels={f"machine-{machine.pk}": "Asked for a due reading", "weather": "Asked for a due reading"})


def refreshes_asked():
    """A refresh of a connection and of twenty kinds asked of the controller
    and not yet answered, as a burst of events leaves them."""

    kinds = [kind for kind, spec in OBSERVATIONS.items() if spec.provider and not reader_of(kind)][:20]
    cadence.ask(connection=PORTAINER)
    cadence.ask(reads=kinds)


def glance_unanswered():
    """The machine's reading 22 minutes old with the refresh the dashboard
    asked for five minutes ago still unanswered, so it is out of date."""

    glance_aged()
    machine = ManagedResource.objects.get(key=MACHINE)
    cadence.ask(panels={f"machine-{machine.pk}": "Asked for a due reading"})
    DashboardRefreshRequest.objects.update(requested_at=now() - timedelta(minutes=5))

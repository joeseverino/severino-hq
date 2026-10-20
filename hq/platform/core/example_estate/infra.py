"""The machine, every name it serves, the certificate in front of them, and
the connections HQ reads them through."""

from datetime import timedelta

from hq.domains.control_plane.models import (
    DashboardConfiguration,
    DashboardMachine,
    ManagedResource,
    WeatherObservation,
)
from hq.platform.application import readings
from hq.platform.application.inventory_testing import store
from hq.platform.application.report_testing import report_connections
from hq.platform.application.security import cli_principal

from . import network, work
from .names import (
    ADGUARD,
    CLOUDFLARE,
    CONTAINERS,
    DNS,
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
    iso,
    now,
    stamp,
)

CERTIFICATE = "example-wildcard"
CERTIFICATE_EXPIRES = timedelta(days=61)


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
            conditions=[{"type": "Ready", "status": True, "reason": "Reconciled", "message": ""}],
        )


def connections():
    def answering(ref, provider, endpoint, detail, reaches=()):
        return {
            "connection_ref": ref,
            "provider": provider,
            "endpoint": endpoint,
            "reaches": list(reaches),
            "ok": True,
            "probed": True,
            "detail": detail,
        }

    report_connections(
        [
            answering(
                PORTAINER, "portainer", "https://portainer.example.com", "1 of 1 environment reachable.", [MACHINE]
            ),
            answering(DNS, "cloudflare_dns", "https://api.cloudflare.com/client/v4", "1 zone.", [ZONE]),
            answering(CLOUDFLARE, "cloudflare_api", "https://api.cloudflare.com/client/v4", "1 account.", [ZONE]),
            answering(PROXY_REF, "npm", f"http://{LAN}:81", f"{len(SERVICES)} proxy hosts."),
            answering(ADGUARD, "adguard", f"http://{LAN}:3080", f"{len(SERVICES)} rewrites."),
            answering(TAILSCALE, "tailscale", "https://api.tailscale.com", "3 devices."),
            answering(SSH, "ssh", f"{MACHINE}:22", "Host key matches.", [MACHINE]),
            answering(GITHUB, "github_app", "https://api.github.com", f"{len(work.REPOSITORIES)} repositories."),
            answering("example-1password", "onepassword", "https://my.1password.com", "1 vault."),
        ],
        principal=cli_principal(),
        controller_id="example-controller",
    )


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
    """What the host reports of itself: its firewall, perimeter and load."""

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
            "organisation": "Example Broadband",
            "network": "EXAMPLE-RESIDENTIAL",
            "handle": "NET-198-51-100-0-1",
            "country": "US",
            "range": "198.51.100.0/24",
            "read_at": iso(timedelta(hours=3)),
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
    readings.record(
        readings.machine_telemetry(MACHINE),
        {
            "status": "good",
            "summary": "Host load 0.31",
            "metrics": [
                {"label": "Container CPU", "value": "12%", "detail": ""},
                {"label": "Container memory", "value": "48%", "detail": ""},
                {"label": "Docker storage", "value": "41%", "detail": ""},
            ],
        },
        observed_at=now(),
    )
    _weather()


def _weather():
    """The dashboard's forecast, as the weather service words a showery afternoon."""

    point = "40.0000,-90.0000"
    DashboardConfiguration.objects.update_or_create(
        pk=1, defaults={"weather_point": point, "weather_label": "Example City"}
    )
    WeatherObservation.objects.create(
        point=point,
        payload={
            "status": "good",
            "summary": "Example City, EX",
            "metrics": [
                {"label": "Now", "value": "Chance Light Rain Showers", "detail": "This Afternoon"},
                {"label": "Temperature", "value": "58°F", "detail": ""},
                {"label": "Wind", "value": "WSW 10 to 15 mph", "detail": "NWS hourly forecast"},
                {"label": "Alerts", "value": "0", "detail": "active for this point"},
            ],
        },
        observed_at=now() - timedelta(minutes=14),
    )

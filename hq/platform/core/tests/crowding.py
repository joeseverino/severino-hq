"""The crowding the browser layout gate lays on top of the example estate.

The example estate is the homelab HQ is shown with, so its values are the
ones a person reads. A page can lay out with those and still break with what
an operator's estate grows to hold: names of 40 and more characters,
hyphenated so a browser may break them only at a hyphen, a machine serving a
dozen hostnames and running twenty containers, a request path with every kind
of hop, many findings, held requests and long histories. This adds those, and
only those, to the example estate, as the ``crowded`` set. It lives in the
test tree, so the demo and every set ``seeds`` names never load it.

Every name is example.*; every address is a documentation or shared-address
range (192.0.2.0/24, 198.51.100.0/24, 100.64.0.0/10).
"""

import json
from datetime import time, timedelta

from django.utils import timezone

from hq.domains.assets.models import Asset
from hq.domains.calendars.models import Entry
from hq.domains.contacts import inbox
from hq.domains.control_plane.models import (
    CapabilityRule,
    DashboardConfiguration,
    DashboardMachine,
    ManagedResource,
    OperationRequest,
    ProviderInventory,
)
from hq.domains.control_plane.observations.github import PROFILE_KIND, REPOSITORY_KIND
from hq.domains.control_plane.observations.portainer import IMAGE_KIND as PULLED_KIND, STACK_KIND
from hq.domains.control_plane.observations.public_registry import (
    IMAGE_KIND as PUBLISHED_KIND,
    VULNERABILITY_KIND,
)
from hq.domains.projects.models import Project
from hq.platform.application import readings
from hq.platform.application.approvals import is_held
from hq.platform.application.capabilities import execute_capability
from hq.platform.application.inventory_testing import store
from hq.platform.application.payload_words import changed_words
from hq.platform.application.pins import SERVICE as PINNED_SERVICE, toggle
from hq.platform.application.references import Kind
from hq.platform.application.report_testing import report_connections
from hq.platform.application.security import Capability, Principal
from hq.platform.application.services import CONTAINER_KIND
from hq.platform.application.tests.writers import keyed, runbook
from hq.platform.core import seeds
from hq.platform.core.example_estate.records import SERVER_ASSET
from hq.platform.core.models import AuditLog
from hq.platform.core.testing import declare

# Hyphenated on purpose: a browser breaks a name at a hyphen, and a name that
# wraps there mid-word is the failure this crowding exists to show.
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
# The agent whose requests wait for a person (`_held`).
AGENT = "an-example-agent-with-a-long-name"
# A record whose history and queued work fill the records list, its own page
# and the controller's: on its own record, so no other page changes.
OPERATED = "an-example-proxy-host-whose-operations-are-recorded"
# The proxy host an agent asks to remove (`_held`).
REMOVED = f"{OPERATED}-and-queued"
# The declared tailnet policy an agent asks to amend (`_held_policy_insert`).
POLICY = "an-example-tailnet-policy-with-a-long-name"
AUDITED = "an-example-proxy-host-with-a-deliberately-long-name"
# A browser's own User-Agent, the longest header value the connection page shows.
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"


def _hostname(index):
    return f"example-hostname-with-a-long-descriptive-name-{index:02d}.example.com"


def _container(index):
    return f"example-background-worker-with-a-long-name-{index:02d}"


def _healthy(key, kind, spec):
    return declare(key, kind, spec, conditions=[{"type": "Ready", "status": True, "reason": "", "message": ""}])


def _proxy(key, domain, forward_host, port):
    return _healthy(
        key,
        "npm.proxy_host",
        {"domain_names": [domain], "forward_scheme": "http", "forward_host": forward_host, "forward_port": port},
    )


def _beside(kind, *records):
    """``records`` added to what the estate's sweep of ``kind`` found."""

    held = ProviderInventory.objects.filter(kind=kind).first()
    if held is None:
        store(kind, *records)
        return
    held.records = [*held.records, *records]
    held.save(update_fields=["records"])


def crowd(owner):
    """The crowding, on top of the example estate ``owner`` owns."""

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
    for hostname in (SERVICE, _hostname(0)):
        toggle(owner, PINNED_SERVICE, hostname)
    _findings()
    _containers()
    _glance()
    _due_on_opening()
    _connections()
    _audit_trail(owner)
    _inventory_pass()
    _unbroken_words(owner)
    _starred()
    _operations()
    _unmanaged()
    _tailnet_policy()
    _held()
    _calendar()
    _messages()


def build(owner):
    """The showcase set, owned by ``owner``, crowded."""

    seeds.get("showcase").plant(owner)
    crowd(owner)


seeds.register("crowded", seeds.Seed(build, seeds.get("showcase").settings))


def _machines():
    declare(
        "example-primary-host",
        "machine",
        {
            "name": MACHINE,
            "role": "Docker host for every example workload, with a role long enough to wrap",
            "addresses": [ADDRESS, TAILNET_ADDRESS],
        },
    )
    declare(
        "example-edge-host",
        "machine",
        {"name": EDGE_MACHINE, "role": "Reverse proxy", "addresses": [EDGE_ADDRESS, EDGE_TAILNET_ADDRESS]},
    )
    # Declared and on no network HQ reads: its state is two words.
    declare(
        "example-printer",
        "machine",
        {"name": "example-printer-with-a-long-name", "role": "Network printer", "addresses": ["192.0.2.77"]},
    )
    now = timezone.now()
    _beside(
        "tailscale.device",
        *(
            {
                "name": name,
                "addresses": [address],
                "dns_name": f"{name}.example.ts.net.",
                "os": "linux",
                "online": True,
                "public_key": f"test-key-{index}",
                "endpoints": [f"{public}:41641"],
                # A port open to one name, and one open to several, which
                # wrap beside the facts on their row unless given a row.
                "reach": [
                    {"port": 22, "who": ["group:an-example-group-of-administrators"]},
                    {
                        "port": 53,
                        "who": [
                            "autogroup:member",
                            "group:an-example-group-of-administrators",
                            "tag:an-example-server-tag",
                            "example@example.com",
                        ],
                    },
                ]
                if name == MACHINE
                else [],
            }
            for index, (name, address, public) in enumerate(
                ((MACHINE, TAILNET_ADDRESS, ADDRESS), (EDGE_MACHINE, EDGE_TAILNET_ADDRESS, EDGE_ADDRESS))
            )
        ),
        {
            "name": "example-operators-laptop-with-a-long-name",
            "addresses": ["100.64.0.20"],
            "dns_name": "example-operators-laptop-with-a-long-name.example.ts.net.",
            "os": "macOS",
            "online": True,
            "user": "example@example.com",
            "public_key": "test-key-laptop",
            "direct_endpoint": "198.51.100.7:41641",
            "last_handshake": (now - timedelta(minutes=2)).isoformat(),
            "active": True,
            "endpoints": ["198.51.100.7:41641"],
        },
    )


def _routes():
    """The seven-hop service, its other names, and a dozen names on one machine."""

    _healthy("crowded-service-dns", "adguard.rewrite", {"domain": SERVICE, "answer": ALIAS})
    _healthy("crowded-alias-dns", "adguard.rewrite", {"domain": ALIAS, "answer": TAILNET_ADDRESS})
    _proxy("crowded-service-proxy", SERVICE, FRONTEND, 8080)
    _proxy("crowded-alias-proxy", ALIAS, FRONTEND, 8080)
    # Other names for the service: each answers with the service's own name.
    for index in range(2):
        _healthy(
            f"crowded-service-alias-{index}",
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
    for index in range(HOSTNAMES):
        _proxy(f"crowded-host-{index:02d}", _hostname(index), ADDRESS, 9000 + index)
        _healthy(
            f"crowded-host-dns-{index:02d}",
            "adguard.rewrite",
            {"domain": _hostname(index), "answer": TAILNET_ADDRESS},
        )
    _healthy(
        "crowded-wildcard",
        "tls.certificate",
        {
            "certificate_name": "example-wildcard-certificate-with-a-long-name",
            "domains": ["example.com", "*.example.com"],
            "install_on": ["crowded-service-proxy"],
            "renewal_window_days": 30,
        },
    )


def _findings():
    """Records a sweep skipped: one finding each, with evidence to lay out."""

    now = timezone.now()
    for index in range(FINDINGS + 3):
        record = declare(
            f"crowded-skipped-{index:02d}",
            "adguard.rewrite",
            {"domain": f"a-rewrite-a-sweep-skipped-with-a-long-name-{index:02d}.example.com", "answer": ADDRESS},
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
    _beside(
        CONTAINER_KIND,
        {
            "name": FRONTEND,
            "host": MACHINE,
            "stack": FRONTEND,
            "image": "registry.example.com/example/a-web-frontend-image-with-a-long-name:2026.09.28",
            "state": "running",
            "ports": [8080],
            "connection_ref": PORTAINER,
        },
        *(
            {
                "name": _container(index),
                "host": MACHINE,
                "image": f"registry.example.com/example/a-worker-image-with-a-long-name-{index:02d}:1.2.3",
                "state": "running" if index % 4 else "exited",
                "ports": [9000 + index],
            }
            for index in range(CONTAINERS - 1)
        ),
    )
    _hardening()
    _behind()


def _behind():
    """The frontend's registry publishes a newer build, and OSV fixes a serious
    finding in the build it runs: its row and its page each say a version it
    changes into."""

    image, tag = "registry.example.com/example/a-web-frontend-image-with-a-long-name", "2026.09.28"
    running = f"sha256:{'a' * 64}"
    now = timezone.now().isoformat()
    _beside(
        PULLED_KIND,
        {
            "connection_ref": PORTAINER,
            "host": MACHINE,
            "id": f"sha256:{'b' * 64}",
            "tags": [f"{image}:{tag}"],
            "digests": [f"{image}@{running}"],
            "containers": [{"container": FRONTEND, "reference": f"{image}:{tag}"}],
        },
    )
    _beside(
        PUBLISHED_KIND,
        {
            "image": image,
            "tags": [tag, "2026.10.30"],
            "digests": {tag: running, "2026.10.30": f"sha256:{'c' * 64}"},
            "read_at": now,
        },
    )
    _beside(
        VULNERABILITY_KIND,
        {
            "digest": f"{image}@{running}",
            "checked": 40,
            "findings": [
                {
                    "id": "GHSA-0000-0000-0000",
                    "package": "an-example-package-with-a-long-name",
                    "installed": "1.2.3",
                    "fixed": ["1.2.4", "1.3.0"],
                    "severity": "high",
                    "summary": "An example finding with a fixed version",
                    "url": "https://osv.dev/vulnerability/GHSA-0000-0000-0000",
                }
            ],
            "read_at": now,
        },
    )


def _hardening():
    """The frontend, watched, failing every check a compose file can meet: its
    page carries the whole compose change, and the action items a button each."""

    declare(
        "crowded-frontend",
        CONTAINER_KIND,
        {"connection_ref": PORTAINER, "host": MACHINE, "name": FRONTEND},
    )
    source = "/opt/example-applications/a-web-frontend-with-a-long-name/configuration"
    _beside(
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
    )
    _beside(STACK_KIND, *_projects())


# Short project directories, each ending in a hyphenated name a cell draws whole
# where its column has room, beside one with no hyphen.
PROJECT_NAMES = ("example-connect", "example2-proxy", "example2-proxy-web", "example-id", "example-hq", "example-labs")


def _projects():
    """The frontend's compose project, and a machine's worth of short ones
    whose directories the Compose projects table draws in a wide column."""

    return [
        {
            "connection_ref": PORTAINER,
            "host": MACHINE,
            "name": FRONTEND,
            "config_files": ["/opt/example-applications/a-web-frontend-with-a-long-name/docker-compose.production.yml"],
        },
        *(
            {
                "connection_ref": PORTAINER,
                "host": MACHINE,
                "name": name,
                "source": "compose",
                "working_dir": f"/opt/apps/{name}",
                "config_files": [f"/opt/apps/{name}/compose.yaml"],
            }
            for name in (*PROJECT_NAMES, "example")
        ),
    ]


def _glance():
    """Both long-named machines on the dashboard beside the estate's, and the
    forecast with its alerts, every panel read."""

    now = timezone.now()
    for position, key in enumerate(("example-primary-host", "example-edge-host"), start=1):
        DashboardMachine.objects.create(machine=ManagedResource.objects.get(key=key), position=position)
        readings.record(
            readings.machine_telemetry(key),
            {
                "key": key,
                "docker": {
                    "containers": 14,
                    "cpu_percent": 37.0,
                    "cores": 8,
                    "memory_used": 13_900_000_000,
                    "memory_total": 17_179_869_184,
                    "storage": 64_000_000_000,
                },
            },
            observed_at=now,
        )
    forecast = readings.stored(readings.weather(DashboardConfiguration.objects.get().weather_point))
    alerts = [
        {"event": "Heat Advisory", "headline": "Heat Advisory until evening", "severity": "Severe"},
        {"event": "Air Quality Alert", "severity": "Moderate"},
    ]
    readings.record(forecast.key, {**forecast.value, "alerts": alerts}, observed_at=forecast.observed_at)


def _due_on_opening():
    """What the dashboard's queue says GitHub holds, old enough to read again,
    so the dashboard draws the ask it posts once it is shown."""

    old = timezone.now() - timedelta(hours=3)
    ProviderInventory.objects.filter(kind=REPOSITORY_KIND).update(observed_at=old, updated_at=old)


def _connections():
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
                # On a catalogd machine: the row says which, as a link in
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
            {
                "connection_ref": "an-example-proxy-behind-single-sign-on",
                "provider": "npm",
                "endpoint": "https://proxy.example.com",
                "ok": False,
                "probed": True,
                "detail": "The address answered with a sign-in page at sso.example.com, "
                "not the API. Use the provider's direct API address.",
                "failure": "address",
            },
        ],
        controller_id="example-controller",
    )


def _operations():
    now = timezone.now()
    operated = _proxy(OPERATED, f"{OPERATED}.example.com", ADDRESS, 9100)
    queued = _proxy(REMOVED, f"{REMOVED}.example.com", ADDRESS, 9101)

    def asked(resource, action, state, message, interface="web", **fields):
        return OperationRequest.objects.create(
            resource=resource,
            action=action,
            state=state,
            requested_actor="an-example-operator-with-a-long-name@example.com",
            requested_interface=interface,
            idempotency_key=f"{resource.key}-{action}-{state}-{message[:12]}",
            result={"message": message} if message else {},
            **fields,
        )

    for index, (state, message) in enumerate(
        (
            ("succeeded", f"Applied the forward host {ADDRESS} to {_hostname(HOSTNAMES - 1)} and read it back"),
            ("failed", "The proxy manager refused the change: the certificate named is not one it holds"),
            ("succeeded", "Reconciled with nothing to change"),
        )
    ):
        asked(operated, "reconcile", state, message, interface="web" if index else "controller", completed_at=now)
    asked(operated, "restart", "queued", "")
    asked(
        queued,
        "reconcile",
        "claimed",
        "",
        claimed_by="example-controller",
        claimed_at=now,
        lease_expires_at=now + timedelta(minutes=5),
    )


def _unmanaged():
    """Names a provider answers for that nothing in HQ declares."""

    _beside(
        "adguard.rewrite",
        *(
            {
                "domain": f"an-unmanaged-name-with-a-long-description-{index}.example.com",
                "answer": f"192.0.2.{50 + index}",
            }
            for index in range(3)
        ),
    )


def _tailnet_policy():
    """Services the tailnet serves and the shell access it grants, in the policy the estate's tailnet reads."""

    group = "group:an-example-group-of-administrators"
    held = ProviderInventory.objects.get(kind="tailscale.policy")
    (policy,) = held.records
    held.records = [
        {
            **policy,
            "groups": [*policy["groups"], {"name": group, "members": ["example@example.com"]}],
            "grants": [*policy["grants"], {"src": [group], "dst": ["tag:server"], "ip": ["tcp:443"]}],
            "services": [
                {
                    "name": f"svc:an-example-tailnet-service-with-a-long-name-{index}",
                    "addresses": [f"100.100.0.{index + 1}", f"fd7a:115c:a1e0::{index + 1}"],
                    "ports": ["tcp:443", "tcp:8443"],
                    "comment": "An example service the tailnet serves, said at a real comment's length",
                }
                for index in range(3)
            ],
            "ssh_rules": [
                {
                    "src": [group],
                    "dst": ["tag:server"],
                    "users": ["root", "an-example-service-account"],
                    "action": "check",
                },
                {
                    "src": ["autogroup:member"],
                    "dst": ["autogroup:self"],
                    "users": ["autogroup:nonroot"],
                    "action": "accept",
                },
            ],
        }
    ]
    held.save(update_fields=["records"])


def _held():
    """An agent's docs import waiting for a person: a list of records and a
    flag, the payload a held request most often carries. Beside it the same
    agent's removal of a proxy host, whose decision card lists every field
    the removal would take."""

    CapabilityRule.objects.create(subject=AGENT, capability="documentation.sync", rule="approve")
    agent = Principal(AGENT, "mcp", frozenset({Capability.READ, Capability.SYNC_DOCUMENTATION}))
    answer = execute_capability(
        "documentation.sync",
        keyed(
            {
                "manifest": [
                    {**runbook(f"rb-an-example-runbook-with-a-long-name-{index}"), "title": "An example runbook"}
                    for index in range(12)
                ],
                "prune_orphans": True,
                "confirm_prune": True,
            }
        ),
        principal=agent,
    )
    if not is_held(answer):
        raise AssertionError(f"the agent's docs import was answered {answer}, not held for a person")
    CapabilityRule.objects.create(subject=AGENT, capability="infrastructure.resource.remove", rule="approve")
    remover = Principal(AGENT, "mcp", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
    answer = execute_capability(
        "infrastructure.resource.remove",
        keyed({"reason": "An example reason for removing the proxy host, long enough to wrap"}),
        principal=remover,
        target=REMOVED,
    )
    if not is_held(answer):
        raise AssertionError(f"the agent's removal was answered {answer}, not held for a person")
    _held_policy_insert(remover)


def _policy_grant(index: int) -> dict:
    return {"src": [f"group:an-example-group-{index}"], "dst": ["tag:server"], "ip": ["tcp:443"], "via": ["tag:exit"]}


def _held_policy_insert(agent: Principal) -> None:
    """An amendment that inserts one grant in the middle of eight and widens
    one test, so its diff reads one grant added and one test changed, never
    every later grant shifted."""

    tests = [{"src": f"user-{index}@example.com", "accept": ["tag:server:443"]} for index in range(3)]
    declared = {"grants": [_policy_grant(index) for index in range(8)], "tests": tests}
    declare(POLICY, "tailscale.policy", {"document": json.dumps(declared, indent=2)})
    grants = declared["grants"]
    inserted = {"src": ["group:an-example-group-of-administrators"], "dst": ["tag:server"], "ip": ["*"]}
    asked = {
        "grants": [*grants[:4], inserted, *grants[4:]],
        "tests": [*tests[:2], {**tests[2], "accept": ["tag:server:443", "tag:server:22"]}],
    }
    CapabilityRule.objects.create(subject=agent.actor, capability="infrastructure.resource.update", rule="approve")
    answer = execute_capability(
        "infrastructure.resource.update",
        keyed(
            {
                "key": POLICY,
                "kind": "tailscale.policy",
                "spec": {"document": json.dumps(asked, indent=2)},
                "enabled": True,
            }
        ),
        principal=agent,
        target=POLICY,
    )
    if not is_held(answer):
        raise AssertionError(f"the agent's policy amendment was answered {answer}, not held for a person")


def _starred():
    """Repositories the owner's GitHub account stars, with long names,
    releases and advisories, beside the estate's."""

    now = timezone.now()
    advisory = {
        "id": "GHSA-abcd-efgh-ijkl",
        "url": "https://github.com/advisories/GHSA-abcd-efgh-ijkl",
        "severity": "high",
        "summary": "An example advisory whose summary runs to the length a real one does, and past it",
        "published_at": (now - timedelta(days=3)).isoformat(),
    }
    watched = [
        {
            "name": f"an-example-organization/an-example-repository-with-a-long-name-{index}",
            "url": f"https://github.com/example/an-example-repository-with-a-long-name-{index}",
            "description": "An example repository description long enough to wrap across a line or two",
            "starred_at": (now - timedelta(days=index + 1)).isoformat(),
            "release": {
                "tag": f"v1.{index}.0-an-example-release-candidate",
                "url": f"https://github.com/example/an-example-repository-with-a-long-name-{index}/releases",
                "published_at": (now - timedelta(days=index)).isoformat(),
            }
            if index % 2
            else None,
            "advisories": [advisory] * index,
        }
        for index in range(4)
    ]
    held = ProviderInventory.objects.get(kind=PROFILE_KIND)
    (profile,) = held.records
    held.records = [{**profile, "watched": [*profile["watched"], *watched]}]
    held.save(update_fields=["records"])


def _audit_trail(owner):
    """One change to a proxy host, among the rest of its operation and the
    object's history: the audit page's every table, with long words in it."""

    def logged(action, message, **fields):
        defaults = {"user": owner, "object_type": "proxy_host", "object_repr": AUDITED}
        return AuditLog.objects.create(action=action, message=message, **(defaults | fields))

    # The last thing a connection did names a resource as long as a real
    # container's: a stack prefix, a service and a replica.
    AuditLog.objects.create(
        action=AuditLog.Action.UPDATED,
        object_type="container",
        object_repr="an-example-edge-machine-agent-stack-an-example-agent-service-1",
        connection=PORTAINER,
    )
    operation = "an-example-operation-0123456789abcdef"
    # Two locations of its advanced config, each known by its path.
    root, api = {"path": "/", "allowed_sources": ["group:example"]}, {"path": "/api", "forward_host": ""}
    moved = [{**root, "allowed_sources": ["group:example", "group:elsewhere"]}, {**api, "forward_host": "203.0.113.30"}]
    for index in range(3):
        logged(
            AuditLog.Action.UPDATED,
            f"Changed the forward host of {_hostname(index)} to {ADDRESS}",
            object_id=f"another-{index}",
            object_repr=_hostname(index),
            operation_id=operation,
        )
        logged(AuditLog.Action.CREATED, f"Created {AUDITED} from a request on {_hostname(index)}", object_id="7")
    logged(
        AuditLog.Action.UPDATED,
        f"Pointed {AUDITED} at a new upstream",
        object_id="7",
        operation_id=operation,
        metadata={
            "changes": {
                "forward_host": [ADDRESS, EDGE_ADDRESS],
                "domain_names": [[_hostname(0)], [_hostname(0), _hostname(1)]],
                "certificate": ["an-example-wildcard-certificate", "an-example-certificate-with-a-long-name"],
                # A record field, as each value in it that moved, by where it is, as the log writes it.
                "advanced_config": {
                    row.label: [row.before, row.after]
                    for row in changed_words({"locations": [root, api]}, {"locations": moved})
                },
            },
            "requested_from": SERVICE,
        },
    )


def _inventory_pass():
    """A controller pass that changed ninety records under an id no row of it
    names, the shape whose fold counts the rest, and a change an agent made
    that a person approved: the history's longest Who."""

    operation = "an-example-inventory-pass-0123456789abcdef"
    controller = {"operation": "bridge.inventory", "interface": "controller", "actor": "controller"}
    AuditLog.objects.bulk_create(
        AuditLog(
            action=AuditLog.Action.UPDATED,
            object_type="Managed resource",
            object_id=f"record-{index}",
            object_repr=f"{_hostname(index % 3)}-record-{index}",
            operation_id=operation,
            metadata={**controller, "subject": f"record-{index}"},
        )
        for index in range(90)
    )
    AuditLog.objects.create(
        action=AuditLog.Action.CREATED,
        object_type="Infrastructure operation",
        object_repr=f"{AUDITED}: reconcile",
        metadata={"actor": AGENT, "approved_by": "an-example-operator"},
    )


def _unbroken_words(owner):
    """What a history holds with no seam in it: a sign-in's subject, a link
    quoted in a message, and a run of containers seen one after another, each
    named after its stack and machine."""

    AuditLog.objects.create(
        action=AuditLog.Action.UPDATED,
        user=owner,
        object_type="user",
        object_repr="oidc:" + "0123456789abcdef" * 4,
        message="Signed in through the identity provider",
    )
    AuditLog.objects.create(
        action=AuditLog.Action.UPDATED,
        user=owner,
        object_type="action_item",
        object_repr="An example action item",
        message="Marked read from https://hq.example.test/action-items/?q=has+not+been+read&status=&source=",
    )
    for index in range(3):
        AuditLog.objects.create(
            action=AuditLog.Action.CREATED,
            object_type="container",
            object_repr=f"{MACHINE}-{_container(index)}-{index + 1}",
            connection=PORTAINER,
        )


def _calendar():
    """A full month: a trip over a weekend, a timed visit with a long name and
    a place, a day too busy for its cell, a weekly class, a long title in the
    last column, beside the month's edge, and a yearly service of the server
    its asset's page names."""

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
    asset = Asset.objects.get(slug=SERVER_ASSET)
    Entry.objects.create(
        title="Example yearly service with a long descriptive title",
        starts_on=today + timedelta(days=3),
        repeat="yearly",
        about=Kind.ASSET.of(asset.slug),
        about_name=asset.item_name,
    )


def _messages():
    """A message at the widest moment a list writes, another year's at a
    two-digit hour, from the longest address."""

    first = ProviderInventory.objects.get(kind=inbox.READING).records[0]
    moment = "2025-12-28 17:58:00"
    _beside(
        inbox.READING,
        {
            **first,
            "id": 99,
            "created_at": moment,
            "updated_at": moment,
            "name": "Example Person",
            "email": "bartholomew.featherstonehaugh@example.com",
            "message": "Hello from the contact form.",
            "status": "unread",
        },
    )

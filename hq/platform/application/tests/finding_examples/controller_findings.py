from collections.abc import Callable
from datetime import timedelta

from hq.domains.control_plane.models import ManagedResource
from hq.platform.core.testing import connection, declare

from ...refused_reports import note
from ..inventory_records import kind_read
from ..test_findings import observed
from . import AT, NOW, Ago, Example, Examples, Raised, ago, delivery_findings, stored

REWRITE, CONTAINER, MACHINE = "adguard.rewrite", "portainer.container", "machine"
DEGRADED = [{"type": "Degraded", "status": True, "reason": "Failed", "message": "The provider refused it."}]
RECONCILE, UPDATE = "infrastructure.resource.reconcile", "infrastructure.resource.update"
REFRESH = "infrastructure.controller.refresh"
# HQ deploys: a declaration of what HQ watches, whose production is its own report.
DELIVERY = "github.delivery"
DELIVERED = {
    "repository": "example/host",
    "workflow": ".github/workflows/compose.yml",
    "branch": "main",
    "production": "Every extension's latest admission, confirmed on GitHub",
}
UNCONFIRMED = "example.alpha: aaaaaaa is live, not yet confirmed on GitHub"
REPORTS_ITSELF = [{"type": "Degraded", "status": True, "reason": "Reported", "message": f"{UNCONFIRMED}."}]


def held(key: str, kind: str = REWRITE, *, seen: Ago | None = NOW, spec=None, status=None, **row):
    """A record HQ declares (a rewrite unless ``kind`` says), last seen ``seen`` ago or never, with ``row`` over it."""

    def make():
        resource = declare(key, kind, spec or {"domain": f"{key}.example.com", "answer": "10.0.0.10"})
        if seen is not None:
            observed(resource, AT - seen.age, status=status)
        if row:
            ManagedResource.objects.filter(pk=resource.pk).update(**row)

    return make


def box(name: str, seen: Ago = NOW, **spec):
    """A container on example-host."""

    host = {"connection_ref": "example-portainer", "host": "example-host", "name": name}
    return held(f"example-host-{name}", CONTAINER, seen=seen, spec={**host, **spec})


def estate(*rows: Callable[[], object], raises: tuple[Raised | str, ...] = (), **readings) -> Example:
    return Example(readings=readings, given=lambda: [row() for row in rows], raises=raises)


def machine(*containers: Callable[[], object], read: bool = True, raises: tuple[Raised | str, ...] = ()) -> Example:
    """example-host, read through its Portainer environment (or a read of it that failed), its containers read now."""

    environment = {"connection_ref": "example-portainer", "id": 2, "name": "example-env", "host": "example-host"}
    return estate(
        held("example-host", MACHINE, seen=None, spec={"name": "example-host", "addresses": ["192.0.2.30"]}),
        *containers,
        lambda: kind_read(CONTAINER, AT),
        raises=raises,
        **{
            "portainer.environment": ({**environment, "local": True, "type": "docker", "status": "up"},)
            if read
            else stored(reachable=False, error="unreachable")
        },
    )


def zone(key: str, seen: Ago | None = NOW):
    return held(key, "cloudflare.zone", seen=seen, spec={"zone": f"{key}.example", "connection_ref": "example-dns"})


def dns(key: str, kind: str, priority: int | None):
    """A DNS record a read gave back whole but for the priority it declares."""

    said = {"zone": "example.com", "name": f"{key}.example.com", "record_type": kind, "content": "mx.example.com"}
    return held(key, "cloudflare.dns_record", spec={**said, "priority": priority}, status=said)


def offering(title: str, press: str = RECONCILE) -> tuple[Raised]:
    """One finding, offering ``press``."""

    return (Raised(title=title, presses=(press,)),)


HEALTHY = estate(held("fine"), held("also-fine"))
STALE = ago(days=3)
SKIPPED = "skipped.example.com at 10.0.0.10 was missing from HQ's last read"
EMPTY = "Portainer's example-env environment on example-host lists no containers"

EXAMPLES = (
    Examples(
        "report-refused",
        fires=(
            Example(
                given=lambda: note("inventory", "example-controller", 422, "/adguard.rewrite/ok: not a boolean"),
                raises=("HQ refuses the controller's inventory report from example-controller",),
            ),
        ),
        quiet=(Example(),),
    ),
    Examples(
        "connections-lost",
        fires=(
            Example(
                given=lambda: connection(
                    "cloudflare_dns", "example-dns", reachable=False, unreported_at=AT - timedelta(hours=1)
                ),
                raises=offering("The controller on example-controller reports no connections", REFRESH),
            ),
        ),
        quiet=(Example(),),
    ),
    Examples(
        "work-that-keeps-failing",
        fires=(
            Example(
                given=lambda: connection("ssh", "example-ssh", failing_steps=[{"step": "Deploy", "reason": "exit 1"}]),
                raises=("Work through example-ssh keeps failing",),
            ),
        ),
        quiet=(Example(given=lambda: connection("ssh", "example-ssh")),),
    ),
    Examples(
        "controller-sweep-stale",
        fires=(
            estate(
                lambda: connection("cloudflare_dns", "example-dns"),
                lambda: connection("adguard", "example-adguard"),
                held("old", seen=STALE),
                zone("old-zone", seen=STALE),
                raises=offering("The controller on example-controller stopped reading", REFRESH),
            ),
        ),
        quiet=(estate(held("old", seen=STALE)),),
    ),
    Examples(
        "environment-read-empty",
        fires=(
            machine(box("web", ago(hours=3)), box("db", ago(hours=3)), raises=offering(EMPTY, REFRESH)),
            # Unread, the environment is named by its machine.
            machine(
                box("web", ago(hours=3)),
                read=False,
                raises=offering("Portainer lists no containers on example-host", REFRESH),
            ),
        ),
        # The environment answered, so the one it no longer lists is a claim about that one.
        quiet=(machine(box("web", ago(hours=3)), box("db")),),
    ),
    Examples(
        "skipped-by-a-sweep",
        fires=(
            estate(held("skipped", seen=ago(hours=6)), held("swept"), raises=offering(SKIPPED)),
            # HQ cannot create a container again.
            machine(
                box("web", ago(hours=3)), box("db"), raises=offering("web was missing from HQ's last read", UPDATE)
            ),
            # Nor one that only says what HQ watches, so nothing is pressed.
            estate(
                held("delivery", DELIVERY, seen=ago(hours=6), spec=DELIVERED),
                lambda: kind_read(DELIVERY, AT),
                raises=(Raised(title="HQ deploys was missing from HQ's last read"),),
            ),
        ),
        quiet=(
            HEALTHY,
            # Nobody asked for one switched off to be true.
            estate(held("skipped", seen=ago(hours=6), enabled=False), held("swept")),
            machine(box("web", ago(hours=3), on_demand=True), box("db")),
        ),
    ),
    Examples(
        "kind-never-swept",
        fires=(
            estate(
                held("only", seen=STALE),
                raises=offering("Internal DNS records have not been read for 3\xa0days", REFRESH),
            ),
            estate(
                held("never", seen=None, observed_generation=1),
                raises=offering("Internal DNS records have never been read", REFRESH),
            ),
        ),
        quiet=(
            HEALTHY,
            # A kind no sweep reads, and a kind read that HQ declares nothing of.
            estate(held("printer", MACHINE, seen=None, spec={"name": "printer"})),
            Example(readings={REWRITE: stored({"domain": "a.example.com", "answer": "10.0.0.10"}, age=STALE.age)}),
        ),
    ),
    Examples(
        "reporting-a-fault",
        fires=(
            estate(held("broken", generation=2, conditions=DEGRADED), raises=offering("The provider refused it")),
            # What it reports of itself, applied or not, which no config of HQ's differs from.
            estate(
                held("delivery", DELIVERY, spec=DELIVERED, conditions=REPORTS_ITSELF),
                raises=(Raised(title=UNCONFIRMED, evidence=(("What", "HQ deploys"), ("Status", "Has a problem"))),),
            ),
        ),
        # A deploy on its way, or waiting on a reviewer, is not one.
        quiet=(HEALTHY, delivery_findings.RUNNING, delivery_findings.WAITING),
    ),
    Examples(
        "reconciled-but-still-wrong",
        fires=(estate(held("broken", conditions=DEGRADED), raises=("The provider refused it",)),),
        quiet=(HEALTHY, estate(held("delivery", DELIVERY, spec=DELIVERED, conditions=REPORTS_ITSELF))),
    ),
    Examples(
        "weakly-verified",
        fires=(
            estate(
                held("half", status={"domain": "half.example.com"}),
                raises=offering("HQ could not confirm 1 setting of half.example.com at 10.0.0.10"),
            ),
            # Zero is a value.
            estate(dns("mail", "MX", 0), raises=offering("HQ could not confirm 1 setting of mail.example.com MX")),
        ),
        # A field with no value asserts nothing, and one the provider declares it cannot read is a known gap.
        quiet=(HEALTHY, estate(dns("spf", "TXT", None)), machine(box("web", serves_ports=[8080]))),
    ),
    Examples(
        "never-observed",
        fires=(
            estate(
                held("missing", seen=None),
                held("seen"),
                raises=offering("missing.example.com at 10.0.0.10 has never been found"),
            ),
        ),
        quiet=(
            HEALTHY,
            # Without a sibling read, the gap is the kind's, said once.
            estate(zone("one", seen=None), zone("two", seen=None), held("seen")),
            # A kind no sweep reads is never found by one.
            estate(held("seen", MACHINE, spec={"name": "a"}), held("unseen", MACHINE, seen=None, spec={"name": "b"})),
        ),
    ),
    Examples(
        "resource-unchecked",
        fires=(
            estate(held("silent", conditions=[]), raises=offering("Internal DNS record silent could not be checked")),
            estate(
                held("wildcard", "tls.certificate", seen=ago(days=14), spec={"domains": ["*.example.com"]}),
                raises=offering("TLS certificate wildcard could not be checked"),
            ),
        ),
        quiet=(HEALTHY,),
    ),
)

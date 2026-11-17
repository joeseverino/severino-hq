from hq.domains.control_plane.models import ManagedResource
from hq.domains.control_plane.names import normalized_hostname

from . import Example, Examples, Raised, connection, stored

ZONE, RECORD, TARGET = "cloudflare.zone", "cloudflare.dns_record", "tls.delivery_target"


def record(key: str, zone: str, *, enabled: bool = True, **spec) -> None:
    ManagedResource.objects.create(
        key=key,
        kind=RECORD,
        enabled=enabled,
        spec={
            "zone": zone,
            "name": f"app.{normalized_hostname(zone)}",
            "record_type": "A",
            "content": "192.0.2.10",
            **spec,
        },
    )


def target(key: str, ref: str) -> None:
    ManagedResource.objects.create(key=key, kind=TARGET, spec={"kind": "caddy", "connection_ref": ref, "name": key})


LISTED = ({"zone": "example.com", "connection_ref": "example-dns"},)


def in_zones(*records: dict, read=LISTED, raises: tuple[Raised | str, ...] = ()) -> Example:
    """Records in a DNS connection whose read listed example.com, unless
    ``read`` says otherwise (None: never read)."""

    def given() -> None:
        connection("example-dns", "cloudflare_dns")
        for each in records:
            if each.get("expected"):
                ManagedResource.objects.create(key="expected", kind=ZONE, spec={"zone": each["expected"]})
            else:
                record(**each)

    return Example(readings={} if read is None else {ZONE: read}, given=given, raises=raises)


def targets(*pairs: tuple[str, str], raises: tuple[Raised | str, ...] = ()) -> Example:
    def given() -> None:
        connection("example-edge", "ssh")
        for key, ref in pairs:
            target(key, ref)

    return Example(given=given, raises=raises)


def unconnected() -> None:
    connection("example-edge", "ssh")
    ManagedResource.objects.create(key="unconnected", kind="machine", spec={"name": "example"})


EXAMPLES = (
    Examples(
        "names-nothing",
        fires=(
            # A record in a domain HQ neither expects nor reads, called what every page calls it.
            in_zones(
                {"key": "in-a-domain", "zone": "example.com"},
                {"key": "in-no-domain", "zone": "typo.example"},
                raises=(
                    Raised(
                        title="app.typo.example at 192.0.2.10 is in typo.example, which HQ does not have",
                        presses=("infrastructure.resource.update",),
                    ),
                ),
            ),
            # One that uses a connection no controller has.
            targets(
                ("on-the-edge", "example-edge"),
                ("on-nothing", "example-edge-2"),
                raises=(
                    Raised(
                        title="on-nothing uses a connection no controller has",
                        presses=("infrastructure.resource.update",),
                    ),
                ),
            ),
            # Both links of one record.
            in_zones(
                {"key": "lost", "zone": "typo.example", "connection_ref": "example-other"},
                raises=(
                    Raised(
                        title="app.typo.example at 192.0.2.10 is in typo.example, which HQ does not have",
                        presses=("infrastructure.resource.update",),
                    ),
                    Raised(
                        title="app.typo.example at 192.0.2.10 uses a connection no controller has",
                        presses=("infrastructure.resource.update",),
                    ),
                ),
            ),
        ),
        quiet=(
            # A domain HQ expects is there whether or not it was read.
            in_zones({"expected": "Example.ORG"}, {"key": "in-an-expected-domain", "zone": "example.org"}),
            # Spelled another way is the same domain.
            in_zones({"key": "spelled-differently", "zone": "Example.COM."}),
            # Without a list of the domains, nothing is said.
            in_zones({"key": "in-no-domain", "zone": "typo.example"}, read=None),
            in_zones(
                {"key": "in-no-domain", "zone": "typo.example"}, read=stored(reachable=False, error="The read failed.")
            ),
            # A record switched off.
            in_zones({"key": "in-no-domain", "zone": "typo.example", "enabled": False}),
            # A record that names no connection.
            Example(given=unconnected),
        ),
    ),
)

from hq.domains.control_plane.models import ManagedResource

from ...tone import Tone
from . import Example, Examples, Raised, ago

ADDRESS = "100.64.0.5"
RECORD = {"zone": "example.com", "name": "app.example.com", "record_type": "A", "content": ADDRESS}


def serving() -> None:
    """A declared record that points app.example.com at example-host."""

    ManagedResource.objects.create(
        key="app-record", kind="cloudflare.dns_record", spec=RECORD, conditions=[{"type": "Ready", "status": True}]
    )


def away(*, tags: tuple[str, ...] = ("tag:server",), raises: tuple[Raised, ...] = (), **age: float) -> Example:
    """example-host, which serves app.example.com, last seen by Tailscale ``age`` ago."""

    host = {
        "name": "example-host",
        "online": False,
        "addresses": [ADDRESS],
        "tags": list(tags),
        "last_seen": ago(**age),
    }
    return Example(readings={"tailscale.device": (host,)}, given=serving, raises=raises)


EXAMPLES = (
    Examples(
        "machine-offline",
        # Serving a name, it is serious.
        fires=(away(hours=5, raises=(Raised(title="example-host is offline", severity=Tone.SERIOUS),)),),
        # A restart, and a person's own device asleep, which is no outage.
        quiet=(away(minutes=5), away(tags=(), hours=48)),
    ),
)

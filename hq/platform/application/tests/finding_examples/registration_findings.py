from datetime import timedelta

from hq.domains.control_plane.models import ManagedResource

from ...tone import Tone
from . import AT, Example, Examples, Raised

ZONE = "cloudflare.zone"


def a_declared_zone() -> None:
    ManagedResource.objects.create(
        key="example-zone", kind=ZONE, spec={"zone": "example.com", "connection_ref": "example-dns"}
    )


def registered(days: int, *, auto_renew: bool = False, raises: tuple[Raised | str, ...] = ()) -> Example:
    """A declared zone whose registrar says it runs out in ``days``, a UTC day as a registrar writes it."""

    record = {
        "zone": "example.com",
        "connection_ref": "example-dns",
        "registration": {
            "expires_at": (AT + timedelta(days=days)).date().isoformat(),
            "auto_renew": auto_renew,
            "registrar": "Example Registrar",
        },
    }
    return Example(readings={ZONE: (record,)}, given=a_declared_zone, raises=raises)


EXAMPLES = (
    Examples(
        "registration-lapsing",
        fires=(
            registered(
                40, raises=(Raised(title="example.com expires in 40 days and will not renew", severity=Tone.ATTENTION),)
            ),
            # Serious inside a month.
            registered(10, raises=("example.com expires in 10 days and will not renew",)),
        ),
        quiet=(
            # Every domain expires every year: with auto-renew on that is a calendar.
            registered(40, auto_renew=True),
            registered(200),
        ),
    ),
)

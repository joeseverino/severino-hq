from datetime import timedelta

from hq.domains.control_plane.models import ProviderConnection
from hq.domains.control_plane.observations.r2 import BACKUP_KIND
from hq.platform.core.testing import connection

from . import AT, Example, Examples, ago

REF = "example-backups"


def bucket(
    uploaded: timedelta | None, *, set_up: timedelta = timedelta(days=30), raises: tuple[str, ...] = ()
) -> Example:
    """A backup connection first reported ``set_up`` ago, whose bucket's
    newest archive arrived ``uploaded`` ago, or holds none."""

    def given() -> None:
        found = connection("r2", REF, manages=True)
        ProviderConnection.objects.filter(pk=found.pk).update(created_at=AT - set_up)

    record = {
        "connection_ref": REF,
        "bucket": "example-backups",
        "key": "severino-hq-20260101T031500Z.tar.gz.age",
        "size_bytes": 48_213_904,
        "uploaded_at": ago(seconds=uploaded.total_seconds()).written() if uploaded else "",
        "copies": 90,
    }
    return Example(readings={BACKUP_KIND: [record] if uploaded else []}, given=given, raises=raises)


EXAMPLES = (
    Examples(
        "offsite-backup-late",
        fires=(
            bucket(timedelta(days=3), raises=(f"{REF}: off-site backup is late",)),
            bucket(None, raises=(f"{REF}: no off-site backup yet",)),
        ),
        # Last night's copy, and a connection set up today whose first copy is still to come.
        quiet=(bucket(timedelta(hours=9)), bucket(None, set_up=timedelta(hours=6))),
    ),
)

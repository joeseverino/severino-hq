"""Cloudflare R2: the bucket the controller copies each backup archive into.

Declaration-only beside its reading: HQ declares no record in the bucket. The
controller uploads the host's newest archive when the bucket lacks it and
reads back the newest copy (docs/Backup.md, Off-site copy).
"""

from ..connection_shapes import S3_BUCKET
from ..observations import r2 as readings
from ..permissions import Cloudflare
from ..provider_spec import ConnectionKind

OBSERVATIONS = readings.OBSERVATIONS

CONNECTIONS = {
    "r2": ConnectionKind(
        "Cloudflare R2 backups",
        "scoped",
        S3_BUCKET,
        create=(
            "An R2 API token in the Cloudflare dashboard, under R2, Manage API tokens, with Object Read & Write "
            "on the backup bucket alone: its Access Key ID, Secret Access Key and the account's S3 endpoint."
        ),
        create_url="https://developers.cloudflare.com/r2/api/tokens/",
        writes=(Cloudflare.R2_OBJECT_WRITE,),
        gives="An off-site copy of each nightly backup, in a bucket whose lock refuses overwrite and delete.",
    )
}

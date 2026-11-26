"""Readings from an R2 bucket that holds HQ's off-site backups."""

from pydantic import ConfigDict, Field

lazy from hq.platform.application.labels import human_bytes
lazy from hq.platform.application.ui import counted

from .contract import ObservationRecord, ObservationSpec, Stamp

BACKUP_KIND = "r2.backup"


class R2BackupRecord(ObservationRecord):
    """The newest backup archive in a connection's bucket, as the bucket's
    own listing states it: the r2.backup reading's one record per connection.
    The controller uploads the host's newest archive first when the bucket
    lacks it, so this is the last off-site copy."""

    model_config = ConfigDict(extra="ignore", frozen=True, json_schema_extra={"additionalProperties": False})

    connection_ref: str = Field(description="The connection whose bucket holds it.")
    bucket: str = Field(max_length=63, description="The bucket's name.")
    key: str = Field(max_length=1024, description="The newest archive's object key, its name on the host.")
    size_bytes: int = Field(ge=0, description="The archive's size in bytes.")
    uploaded_at: Stamp = Field(description="When the bucket last wrote the object (LastModified).")
    copies: int = Field(ge=1, description="The backup archives the bucket holds.")


OBSERVATIONS: tuple[ObservationSpec, ...] = (
    ObservationSpec(
        BACKUP_KIND,
        "r2",
        "Off-site backup",
        R2BackupRecord,
        title=lambda record: str(record.get("key", "")),
        describe=lambda record: (
            f"{human_bytes(record.get('size_bytes', 0))} · {counted(record.get('copies', 0), 'copy', 'copies')} kept"
        ),
        relation="Off-site backup in",
    ),
)

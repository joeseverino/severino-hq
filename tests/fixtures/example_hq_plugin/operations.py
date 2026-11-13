"""The example's automation procedures, supplied as an extension supplies its own.

Three, so a seed can hold one waiting, one working and one whose worker
stopped at once: a procedure has one active run at a time. Each input has a
default for every field, so a seed can ask for a run of one it knows only by
its manifest.
"""

from typing import Annotated, Any

from pydantic import Field

from hq_sdk.automation import OperationManifest
from hq_sdk.capabilities import StrictCommand

CACHE_FLUSH = "example.cache_flush"
DISK_REPORT = "example.disk_report"
PACKAGE_UPGRADE = "example.package_upgrade"
REVISION = "0123456789abcdef0123456789abcdef01234567"


class CacheFlush(StrictCommand):
    cache: Annotated[str, Field(min_length=1, max_length=60, description="The cache to empty, such as web.")] = "web"


class DiskReport(StrictCommand):
    mount: Annotated[str, Field(min_length=1, max_length=200, description="The mount point to report on.")] = "/"


class PackageUpgrade(StrictCommand):
    security_only: Annotated[bool, Field(description="Whether to apply only security updates.")] = True


def _manifest(**fields: Any) -> OperationManifest:
    return OperationManifest(
        **{
            "revision": REVISION,
            "target_kinds": ("machine",),
            "privilege": None,
            "effect": "infrastructure_change",
            "timeout_seconds": 300,
            "retry": "observe_first",
            **fields,
        }
    )


def operations() -> tuple[OperationManifest, ...]:
    return (
        _manifest(
            id=CACHE_FLUSH,
            playbook="playbooks/cache_flush.yml",
            input_type=CacheFlush,
            preconditions=("The cache's service is running on the machine.",),
            verification=("The cache reports no entries.",),
            recovery=("The cache refills on its own as it is read.",),
        ),
        _manifest(
            id=DISK_REPORT,
            playbook="playbooks/disk_report.yml",
            input_type=DiskReport,
            effect="read",
            timeout_seconds=60,
            retry="safe",
            preconditions=("The mount point exists on the machine.",),
            verification=("The report names the mount's size and use.",),
            recovery=("Nothing changes, so nothing needs recovering.",),
        ),
        _manifest(
            id=PACKAGE_UPGRADE,
            playbook="playbooks/package_upgrade.yml",
            input_type=PackageUpgrade,
            privilege="example.package_admin",
            timeout_seconds=1800,
            retry="never",
            preconditions=("The machine's package index is reachable.",),
            verification=("No upgradable package is left of the kind asked for.",),
            recovery=("Pin the package back to its last version and restart what uses it.",),
        ),
    )

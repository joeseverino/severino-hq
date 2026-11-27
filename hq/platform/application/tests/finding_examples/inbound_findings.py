from datetime import timedelta

from hq.domains.control_plane.models import InboundSource
from hq.platform.core.models import UpstreamReading

from ... import readings
from ...inbound_events import channel
from ...inbound_findings import READERS
from . import AT, Example, Examples, Raised
from .unit_findings import KIND as UNITS, failed

SILENT = "The event listener stopped reporting"


def heard(
    source: InboundSource,
    ago: timedelta | None = None,
    *,
    units: tuple[dict, ...] = (),
    raises: tuple[Raised | str, ...] = (),
) -> Example:
    """``source`` last reported ``ago``, or never, on a machine whose ``units`` were read."""

    def given() -> None:
        if ago is not None:
            key = readings.heartbeat(channel(source))
            readings.record(key, {})
            UpstreamReading.objects.filter(key=key).update(observed_at=AT - ago)

    return Example(readings={UNITS: units} if units else {}, given=given, raises=raises)


BEHIND = "The events Worker is behind HQ's routes"
WORKER = "cloudflare.worker"


def deployed(*tags: str, name: str = "hq-events") -> Example:
    """The Worker ``name`` on the account, serving one version per tag, or
    never deployed; a version's tag is what its deploy gave it."""

    record = {
        "connection_ref": "example-cloudflare",
        "account_id": "0123abcd",
        "name": name,
        "deployment_id": "example-deployment" if tags else "",
        "deployed_at": "2026-01-14T18:30:00Z" if tags else None,
        "versions": [
            {"id": f"example-{number}", "number": number, "percentage": 100 / len(tags), "tag": tag}
            for number, tag in enumerate(tags, 7)
        ],
        "requests": 120,
        "errors": 0,
    }
    return Example(readings={WORKER: [record]}, raises=(BEHIND,) if name == "hq-events" and tags else ())


EXAMPLES = (
    Examples(
        "event-channel-silent",
        fires=(
            heard(InboundSource.WORKER, timedelta(hours=1), raises=(SILENT,)),
            heard(InboundSource.HOST, timedelta(days=30), raises=(SILENT,)),
            # Its unit failed: the failure is the item, and this is said beneath it.
            heard(
                InboundSource.WORKER,
                timedelta(hours=1),
                units=(failed(READERS[InboundSource.WORKER]),),
                raises=(SILENT,),
            ),
        ),
        # Never set up; heard lately.
        quiet=(
            heard(InboundSource.WORKER),
            heard(InboundSource.WORKER, timedelta(minutes=3)),
            heard(InboundSource.HOST, timedelta(minutes=3)),
        ),
    ),
    Examples(
        "events-worker-behind",
        # Deployed without the tag HQ's routes give it, whole or as part of a split.
        fires=(deployed(""), deployed("ffffffffffffffffffffffffffffffff", "")),
        # Never deployed; another Worker deployed untagged.
        quiet=(deployed(), deployed("", name="example-site")),
    ),
)

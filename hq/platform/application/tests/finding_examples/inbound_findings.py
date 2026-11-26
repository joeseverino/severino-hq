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
)

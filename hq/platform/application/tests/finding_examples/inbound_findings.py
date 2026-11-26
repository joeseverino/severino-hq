from datetime import timedelta

from hq.domains.control_plane.models import InboundSource
from hq.platform.core.models import UpstreamReading

from ... import readings
from ...inbound_events import channel
from . import AT, Example, Examples


def heard(source: InboundSource, ago: timedelta | None = None, *, raises: tuple[str, ...] = ()) -> Example:
    """``source`` last reported ``ago``, or never."""

    def given() -> None:
        if ago is not None:
            key = readings.heartbeat(channel(source))
            readings.record(key, {})
            UpstreamReading.objects.filter(key=key).update(observed_at=AT - ago)

    return Example(given=given, raises=raises)


EXAMPLES = (
    Examples(
        "event-channel-silent",
        fires=(heard(InboundSource.WORKER, timedelta(hours=1), raises=("The event listener stopped reporting",)),),
        # Never set up; heard lately; and HQ's host jobs, which speak only as a unit fails.
        quiet=(
            heard(InboundSource.WORKER),
            heard(InboundSource.WORKER, timedelta(minutes=3)),
            heard(InboundSource.HOST, timedelta(days=30)),
        ),
    ),
)

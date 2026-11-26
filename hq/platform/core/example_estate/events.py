"""What the estate was told happened outside it: GitHub's deliveries through
the event listener, one it does not take, and a unit HQ's host saw fail."""

from datetime import timedelta

from django.utils import timezone

from hq.domains.control_plane.models import InboundEvent, InboundOutcome, InboundSource
from hq.domains.control_plane.observations.github import PROVIDER as GITHUB, REPOSITORY_KIND
from hq.domains.control_plane.observations.host import UNIT_FAILED, UNIT_KIND
from hq.platform.application import readings
from hq.platform.application.inbound_events import channel

from . import doors

# Each as a source sends it: who, what, about what, how long ago, and the readings it made due.
EVENTS = (
    (InboundSource.WORKER, GITHUB, "push", "example/hq", timedelta(minutes=4), (REPOSITORY_KIND,)),
    (InboundSource.WORKER, GITHUB, "pull_request", "example/site", timedelta(hours=3), (REPOSITORY_KIND,)),
    (InboundSource.WORKER, GITHUB, "star", "example/hq", timedelta(hours=7), ()),
    (InboundSource.HOST, "host", UNIT_FAILED, "example-upload.service", timedelta(days=2), (UNIT_KIND,)),
)


def told() -> None:
    """Each of ``EVENTS``, stored as the controller's bridge reports it, and
    each channel last heard as its latest event arrived; the listener a
    moment ago, as it says it is listening when nothing happens."""

    now = timezone.now()
    with doors.controller("events"):
        for number, (source, provider, event, subject, age, reads) in enumerate(EVENTS, start=1):
            InboundEvent.objects.create(
                provider=provider,
                event=event,
                subject=subject,
                delivery_id=f"00000000-0000-4000-8000-{number:012d}",
                source=source,
                received_at=now - age,
                applied_at=now - age,
                outcome=InboundOutcome.APPLIED if reads else InboundOutcome.IGNORED,
                reads=list(reads),
            )
    readings.record(readings.heartbeat(channel(InboundSource.WORKER)), {})
    host = min(age for source, *_, age, _reads in EVENTS if source == InboundSource.HOST)
    readings.record(readings.heartbeat(channel(InboundSource.HOST)), {}, observed_at=now - host)

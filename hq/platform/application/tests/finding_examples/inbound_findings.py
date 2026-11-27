from datetime import timedelta

from hq.domains.control_plane.models import InboundSource
from hq.platform.core.models import UpstreamReading
from hq.platform.core.testing import declare

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
    declared: bool = False,
    raises: tuple[Raised | str, ...] = (),
) -> Example:
    """``source`` last reported ``ago``, or never, on a machine whose ``units``
    were read, HQ declaring its reader's unit where ``declared``."""

    def given() -> None:
        if declared:
            declare("example-reader", UNITS, {"unit": READERS[source]})
        if ago is not None:
            key = readings.heartbeat(channel(source))
            readings.record(key, {})
            UpstreamReading.objects.filter(key=key).update(observed_at=AT - ago)

    return Example(readings={UNITS: units} if units else {}, given=given, raises=raises)


BEHIND = "The events Worker is behind HQ's routes"
WORKER = "cloudflare.worker"


def deployed(*tags: str, name: str = "hq-events", answers: list[dict] | None = None) -> Example:
    """The Worker ``name`` on the account, serving one version per tag, or
    never deployed; a version's tag is what its deploy gave it. ``answers``
    are its counts by route and status, where a version binds the dataset."""

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
        "answers": answers,
    }
    return Example(readings={WORKER: [record]}, raises=(BEHIND,) if name == "hq-events" and tags else ())


def answered(*counts: tuple[str, int, int, int], raises: tuple[str, ...] = ()) -> Example:
    """The events Worker, deployed as HQ's routes make it, having answered each
    (route, status, requests over the day, of them in the last hour)."""

    found = deployed(
        answers=[
            {"route": route, "status": status, "requests": requests, "last_hour": last_hour}
            for route, status, requests, last_hour in counts
        ]
    )
    return Example(readings=found.readings, raises=raises)


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
            # HQ declares its reader's unit: the unit's start, pressed.
            heard(
                InboundSource.HOST,
                timedelta(hours=1),
                declared=True,
                raises=(Raised(title=SILENT, presses=("infrastructure.resource.start",)),),
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
    Examples(
        "events-worker-refusing",
        # Every delivery of the last hour refused for its signature, as after
        # a rotation on one side; the listener retrying with an old token.
        fires=(
            answered(
                ("/github", 202, 40, 0),
                ("/github", 401, 9, 9),
                raises=("The events Worker refuses GitHub App's signature",),
            ),
            answered(("/socket", 401, 30, 30), raises=("The events Worker refuses the event listener's token",)),
        ),
        # Fewer than a rotation makes; one accepted beside them; refused only
        # earlier in the day; a route the Worker no longer serves; never counted.
        quiet=(
            answered(("/github", 401, 2, 2)),
            answered(("/github", 202, 1, 1), ("/github", 401, 9, 9)),
            answered(("/github", 401, 9, 0)),
            answered(("/example-retired", 401, 9, 9)),
            deployed(),
        ),
    ),
)

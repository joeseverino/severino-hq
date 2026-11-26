"""A channel that tells HQ of events, gone quiet: HQ then learns of a change
only at its next sweep. One never heard is not set up, which is nothing to raise."""

from hq.domains.control_plane.models import InboundSource

from .finding_model import Finding, FindingEstate, FindingRule, opens
from .tone import Tone
from .unit_findings import stopped_reporting
lazy from .inbound_events import channel_standing
lazy from .routes import reverse

RULE = "event-channel-silent"
# The unit on HQ's machine that runs each channel's reader (deploy/systemd).
READERS = {
    InboundSource.WORKER: "severino-hq-listener.service",
    InboundSource.HOST: "severino-hq-events.service",
}


def _silent(estate: FindingEstate) -> tuple[Finding, ...]:
    return tuple(
        stopped_reporting(
            estate,
            standing.seen_at,
            rule=RULE,
            unit=unit,
            part=str(source),
            title=f"The {standing.named} stopped reporting",
            explanation="Changes outside HQ show only once HQ next reads them.",
            severity=Tone.ATTENTION,
            primary=opens("Open events", reverse("control_plane:events")),
        )
        for source, unit in READERS.items()
        if (standing := channel_standing(source)).silent and standing.seen_at is not None
    )


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        RULE,
        "Event channel silent",
        Tone.ATTENTION,
        _silent,
        gathered=True,
        operator_action="Check that the event listener on HQ's machine is running and reaches the Worker.",
        no_help_reason="HQ cannot start the event listener.",
        bars={Tone.ATTENTION: "a channel that reports events stopped reporting"},
    ),
)

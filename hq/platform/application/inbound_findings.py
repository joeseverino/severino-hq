"""A channel that tells HQ of events, gone quiet: HQ then learns of a change
only at its next sweep. One never heard is not set up, which is nothing to raise."""

from hq.domains.control_plane.models import InboundSource

from .finding_model import Finding, FindingEstate, FindingRule, opens
from .moments import when
from .security import Capability
from .tone import Tone
lazy from .inbound_events import CHANNEL_EVERY, channel_standing
lazy from .routes import reverse

RULE = "event-channel-silent"
# It is about no record or reading, so no subject or scope gates it.
_READ = (str(Capability.READ),)


def _silent(estate: FindingEstate) -> tuple[Finding, ...]:
    found = []
    for source in (source for source in InboundSource if CHANNEL_EVERY[source] is not None):
        standing = channel_standing(source)
        if not standing.silent or standing.seen_at is None:
            continue
        found.append(
            Finding(
                rule=RULE,
                subject="",
                part=str(source),
                read_capabilities=_READ,
                title=f"The {standing.named} stopped reporting",
                severity=Tone.ATTENTION,
                explanation="Changes outside HQ show only once HQ next reads them.",
                evidence=(("Last heard", when(standing.seen_at, now=estate.now)),),
                primary=opens("Open the controller", reverse("control_plane:controller")),
            )
        )
    return tuple(found)


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

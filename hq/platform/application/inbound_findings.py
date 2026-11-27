"""A channel that tells HQ of events, gone quiet: HQ then learns of a change
only at its next sweep. One never heard is not set up, which is nothing to raise.

And the events Worker Cloudflare runs, deployed from other routes than HQ
generates, so it verifies and passes on events by the routes it was deployed with."""

from hq.domains.control_plane.models import InboundSource
from hq.domains.control_plane.observations.cloudflare import WORKER_KIND

from .finding_model import Finding, FindingEstate, FindingRule, opens
from .finding_steps import Do, Run
from .moments import when
from .tone import Tone
from .unit_findings import stopped_reporting
lazy from .inbound_events import channel_standing, events_worker
lazy from .routes import reverse

RULE = "event-channel-silent"
BEHIND = "events-worker-behind"
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


def _behind(estate: FindingEstate) -> tuple[Finding, ...]:
    worker = events_worker()
    if not worker.behind:
        return ()
    nodes = {node.connection_ref: node for node in estate.nodes() if node.kind == "connection" and node.connection_ref}
    node = nodes.get(worker.connection_ref)
    return (
        Finding(
            rule=BEHIND,
            subject=node.id if node is not None else "",
            part="" if node is not None else worker.connection_ref,
            title="The events Worker is behind HQ's routes",
            severity=Tone.ATTENTION,
            explanation="Events for a route HQ added or changed since its deploy are refused or verified the old way.",
            evidence=(
                ("Deployed", when(worker.deployed_at, now=estate.now) if worker.deployed_at else "unknown"),
                *(
                    (f"Version {number}" if number is not None else "Version", tag or "untagged")
                    for number, tag in worker.versions
                ),
            ),
            steps=(
                Run(label="Deploy it from main", command="mise run worker:deploy"),
                Do("Or run the Deploy Worker job on main."),
            ),
            primary=opens("Open events", reverse("control_plane:events")),
            observed_at=worker.standing.sight.observed_at,
            reads=WORKER_KIND,
        ),
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
    FindingRule(
        BEHIND,
        "Events Worker behind",
        Tone.ATTENTION,
        _behind,
        gathered=True,
        operator_action="Deploy the events Worker from main.",
        no_help_reason="HQ holds no credential that deploys the Worker.",
        bars={Tone.ATTENTION: "the deployed Worker carries other routes than HQ generates"},
        clears="the deployed Worker's version carries the routes HQ generates",
    ),
)

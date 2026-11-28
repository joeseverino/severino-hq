"""A channel that tells HQ of events, gone quiet: HQ then learns of a change
only at its next sweep. One never heard is not set up, which is nothing to raise.

And the events Worker Cloudflare runs: deployed from other routes than HQ
generates, so it verifies and passes on events by the routes it was deployed
with, or refusing every request to a route for its signature or token, so a
secret that was rotated on one side only drops what that route carries."""

from hq.domains.control_plane.models import InboundSource
from hq.domains.control_plane.observations.cloudflare import WORKER_KIND

from .finding_model import Finding, FindingEstate, FindingRule, opens
from .finding_steps import ByHand, Do, Why
from .moments import when
from .tone import Tone
from .unit_findings import stopped_reporting
lazy from .inbound_events import channel_standing, events_worker
lazy from .routes import reverse

RULE = "event-channel-silent"
BEHIND = "events-worker-behind"
REFUSING = "events-worker-refusing"
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


def _connection(estate: FindingEstate, ref: str) -> str:
    """The node of the connection the Worker was read through, or ""."""

    return next(
        (node.id for node in estate.nodes() if node.kind == "connection" and node.connection_ref == ref and ref), ""
    )


def _behind(estate: FindingEstate) -> tuple[Finding, ...]:
    worker = events_worker()
    if not worker.behind:
        return ()
    subject = _connection(estate, worker.connection_ref)
    return (
        Finding(
            rule=BEHIND,
            subject=subject,
            part="" if subject else worker.connection_ref,
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
            fixes=(
                ByHand(
                    "Deploy it from the commit production runs",
                    "gh workflow run deploy-worker.yml --ref main",
                    Why.NOT_GRANTED,
                ),
            ),
            primary=opens("Open events", reverse("control_plane:events")),
            observed_at=worker.standing.sight.observed_at,
            reads=WORKER_KIND,
        ),
    )


def _refusing(estate: FindingEstate) -> tuple[Finding, ...]:
    worker = events_worker()
    subject = _connection(estate, worker.connection_ref)
    return tuple(
        Finding(
            rule=REFUSING,
            subject=subject,
            part=route.path,
            title=f"The events Worker refuses {route.named}'s {route.proof}",
            severity=Tone.ATTENTION,
            explanation=f"Every request to {route.path} in the last hour was refused: {route.secret} does not match.",
            evidence=(
                ("Refused, last hour", str(route.unsigned_last_hour)),
                ("Accepted, last hour", str(route.accepted_last_hour)),
                ("Secret", route.secret),
            ),
            fixes=(
                Do(f"Set the Worker's {route.secret} to the one {route.named} uses."),
                ByHand("Upload it", "mise run worker:deploy --with-secrets", Why.NOT_GRANTED),
            ),
            primary=opens("Open events", reverse("control_plane:events")),
            observed_at=worker.standing.sight.observed_at,
            reads=WORKER_KIND,
        )
        for route in worker.routes or ()
        if route.refusing
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
        no_help_reason="HQ cannot tell why the listener does not reach the Worker.",
        bars={Tone.ATTENTION: "a channel that reports events stopped reporting"},
    ),
    FindingRule(
        BEHIND,
        "Events Worker behind",
        Tone.ATTENTION,
        _behind,
        gathered=True,
        operator_action="Run Deploy Worker, which deploys the events Worker from the commit production runs.",
        no_help_reason="HQ holds no credential that deploys the Worker.",
        bars={Tone.ATTENTION: "the deployed Worker carries other routes than HQ generates"},
        clears="the deployed Worker's version carries the routes HQ generates",
    ),
    FindingRule(
        REFUSING,
        "Events Worker refusing",
        Tone.ATTENTION,
        _refusing,
        gathered=True,
        operator_action="Set the Worker secret the route checks to the one its sender uses.",
        no_help_reason="HQ holds no credential that sets the Worker's secrets.",
        bars={Tone.ATTENTION: "every request to a route in the last hour was refused for its signature or token"},
        clears="the route accepts a request again",
    ),
)

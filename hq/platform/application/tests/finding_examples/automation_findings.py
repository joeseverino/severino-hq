from collections.abc import Mapping
from datetime import timedelta

from hq.domains.control_plane.models import OperationRequest, SourceProposal
from hq.platform.core.models import UpstreamReading
from hq.platform.core.testing import declare

from ... import readings
from ...automation_findings import AUTOMATION_UNIT
from ...cadence import automation_heartbeat
from ...tone import Tone
from . import AT, Example, Examples, Raised
from .unit_findings import KIND as UNITS, failed, timer

# A profile a runner connection lends the worker a login as, beside the read one.
ADMIN = "example.admin"


def worker(
    seen: timedelta | None = None,
    *,
    source: bool = True,
    units: tuple[dict, ...] = (),
    profiles: Mapping[str, timedelta | None] | None = None,
    raises: tuple[Raised | str, ...] = (),
) -> Example:
    """HQ's read profile's playbook worker last arrived ``seen`` ago, or
    never, with a playbook source unless not ``source``, on a machine whose
    ``units`` were read; and each of ``profiles``' workers, whose logins the
    newest scan reported, last arrived when it says."""

    def heard(profile: str, ago: timedelta | None) -> None:
        if ago is not None:
            readings.record(automation_heartbeat(profile), {})
            UpstreamReading.objects.filter(key=automation_heartbeat(profile)).update(observed_at=AT - ago)

    def given() -> None:
        if source:
            playbooks = declare("automation-source", "automation.source")
            if profiles:
                scan = OperationRequest.objects.create(
                    resource=playbooks,
                    action=OperationRequest.Action.SCAN,
                    idempotency_key="example-scan",
                    state=OperationRequest.State.SUCCEEDED,
                    completed_at=AT,
                )
                SourceProposal.objects.create(
                    operation=scan,
                    repository="example/playbooks",
                    branch="main",
                    revision="1" * 40,
                    verified=True,
                    verification_reason="valid",
                    reaches=[{"machine": "example-app", "profile": profile} for profile in ("read", *profiles)],
                )
        heard("read", seen)
        for profile, ago in (profiles or {}).items():
            heard(profile, ago)

    return Example(readings={UNITS: units} if units else {}, given=given, raises=raises)


EXAMPLES = (
    Examples(
        "automation-worker-absent",
        fires=(
            worker(raises=("Playbooks are not set up",)),
            worker(
                timedelta(hours=3),
                raises=(Raised(title="Playbooks stopped reporting", severity=Tone.SERIOUS),),
            ),
            # Its unit failed: the failure is the item, and this is said beneath it.
            worker(
                timedelta(hours=3),
                units=(failed(AUTOMATION_UNIT), timer(activates=AUTOMATION_UNIT)),
                raises=(Raised(title="Playbooks stopped reporting", severity=Tone.SERIOUS),),
            ),
            # One profile's worker absent alone.
            worker(
                timedelta(minutes=20),
                profiles={ADMIN: None},
                raises=(f"The {ADMIN} playbook worker has never reported",),
            ),
            worker(
                timedelta(minutes=20),
                profiles={ADMIN: timedelta(hours=3)},
                raises=(Raised(title=f"The {ADMIN} playbook worker stopped reporting", severity=Tone.SERIOUS),),
            ),
            # The read profile's absence is every other's, said once.
            worker(
                timedelta(hours=3),
                profiles={ADMIN: timedelta(hours=3)},
                raises=(Raised(title="Playbooks stopped reporting", severity=Tone.SERIOUS),),
            ),
        ),
        # Where nobody set automation up, and where each arrived within its interval.
        quiet=(
            worker(source=False),
            worker(timedelta(minutes=20)),
            worker(timedelta(minutes=20), profiles={ADMIN: timedelta(minutes=20)}),
        ),
    ),
)

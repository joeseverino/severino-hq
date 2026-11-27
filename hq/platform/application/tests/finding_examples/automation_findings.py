from collections.abc import Mapping
from datetime import timedelta

from hq.domains.control_plane.models import OperationRequest
from hq.platform.core.models import UpstreamReading
from hq.platform.core.testing import declare

from ... import readings
from ...automation_findings import AUTOMATION_UNIT
from ...cadence import automation_heartbeat
from ...tone import Tone
from . import AT, Example, Examples, Raised
from .unit_findings import KIND as UNITS, failed, timer

# A profile HQ asked a run of, beside the read one.
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
    ``units`` were read; and each of ``profiles``' workers, of which a run
    waits, last arrived when it says."""

    def heard(profile: str, ago: timedelta | None) -> None:
        if ago is not None:
            readings.record(automation_heartbeat(profile), {})
            UpstreamReading.objects.filter(key=automation_heartbeat(profile)).update(observed_at=AT - ago)

    def given() -> None:
        if source:
            declare("automation-source", "automation.source")
        for index, profile in enumerate(profiles or {}):
            OperationRequest.objects.create(
                resource=declare(f"example-run-{index}", "automation.operation", {"privilege": profile}),
                action=OperationRequest.Action.RUN,
                idempotency_key=f"example-run-{index}",
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

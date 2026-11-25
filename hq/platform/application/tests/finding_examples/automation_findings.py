from datetime import timedelta

from hq.platform.core.models import UpstreamReading
from hq.platform.core.testing import declare

from ... import readings
from ...automation_findings import AUTOMATION_UNIT
from ...cadence import AUTOMATION_HEARTBEAT
from ...tone import Tone
from . import AT, Example, Examples, Raised
from .unit_findings import KIND as UNITS, failed, timer


def worker(
    seen: timedelta | None = None,
    *,
    source: bool = True,
    units: tuple[dict, ...] = (),
    raises: tuple[Raised | str, ...] = (),
) -> Example:
    """HQ's playbook worker last arrived ``seen`` ago, or never, with a
    playbook source unless not ``source``, on a machine whose ``units`` were read."""

    def given() -> None:
        if source:
            declare("automation-source", "automation.source")
        if seen is not None:
            readings.record(AUTOMATION_HEARTBEAT, {})
            UpstreamReading.objects.filter(key=AUTOMATION_HEARTBEAT).update(observed_at=AT - seen)

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
        ),
        # Where nobody set automation up, and where it arrived within its interval.
        quiet=(worker(source=False), worker(timedelta(minutes=20))),
    ),
)

from hq.platform.core.testing import declare

from ...tone import Tone
from . import NOW, Example, Examples, Raised, ago, stored
from .render_status_findings import KIND as RENDERED, read

KIND = "host.unit"
SERVICE = "severino-hq-example.service"
TIMER = "severino-hq-example.timer"
FOREIGN = "example-upload.service"
SECRETS = "severino-hq-secrets.service"


def service(unit: str = SERVICE, **said) -> dict:
    """A oneshot that last ran well, as the controller reports it."""

    return {
        "unit": unit,
        "load": "loaded",
        "file_state": "static",
        "active": "inactive",
        "sub": "dead",
        "result": "success",
        "main_code": 1,
        "started_at": ago(minutes=31),
        "ended_at": ago(minutes=30),
        "condition": "yes",
        "condition_at": ago(minutes=31),
        "read_at": NOW,
        **said,
    }


def timer(unit: str = TIMER, activates: str = SERVICE, **said) -> dict:
    """A calendar timer waiting for its next elapse."""

    return {
        "unit": unit,
        "load": "loaded",
        "file_state": "enabled",
        "active": "active",
        "sub": "waiting",
        "result": "success",
        "last_trigger_at": ago(minutes=31),
        "next_elapse_at": ago(hours=-23),
        "activates": activates,
        "read_at": NOW,
        **said,
    }


def socket(unit: str, activates: str = SERVICE, **said) -> dict:
    """A unit that does its work while active, a socket listening by default."""

    return {
        "unit": unit,
        "load": "loaded",
        "file_state": "enabled",
        "active": "active",
        "sub": "listening",
        "activates": activates,
        "read_at": NOW,
        **said,
    }


def failed(unit: str = SERVICE, **said) -> dict:
    return service(unit, **{"active": "failed", "sub": "failed", "result": "exit-code", "main_status": 3, **said})


def looping(unit: str = FOREIGN, **said) -> dict:
    """A service HQ does not ship that systemd keeps restarting, waiting out its RestartSec."""

    return {
        "unit": unit,
        "title": "Example upload",
        "load": "loaded",
        "file_state": "disabled",
        "active": "activating",
        "sub": "auto-restart",
        "result": "exit-code",
        "main_code": 1,
        "main_status": 1,
        "restarts": 4127,
        "active_at": ago(minutes=1),
        "ended_at": ago(seconds=30),
        "foreign": True,
        "read_at": NOW,
        **said,
    }


def units(*records: dict) -> Example:
    return Example(readings={KIND: records})


def raising(*records: dict, raises: tuple[Raised | str, ...], **fields) -> Example:
    return Example(readings={KIND: stored(*records, **fields) if fields else records}, raises=raises)


STALLED = f"{TIMER} on example-controller is not starting its job"
LOOPING = "“Example upload” keeps restarting on example-controller"
UNREAD = "HQ cannot check its units on example-controller"


# Units that run: a job, its timer, a template's instance, the controller
# always starting as its own run reads it, and a unit not run on this boot.
RUNNING = units(
    service(),
    timer(),
    service("severino-hq-example-job@example.work.service"),
    service("severino-hq-example-controller.service", active="activating", sub="start", started_at=NOW),
    timer(
        "severino-hq-example-controller.timer",
        "severino-hq-example-controller.service",
        sub="running",
        next_elapse_at="",
    ),
    service("severino-hq-example-later.service", started_at="", ended_at="", condition=""),
)
# A listening or held socket and a mounted mount.
ACTIVE = units(
    service(),
    socket("severino-hq-example.socket", sub="listening"),
    socket("severino-hq-example-held.socket", sub="running"),
    socket("run-example.mount", activates="", sub="mounted"),
)
# A machine that reports no units.
NONE = units()
# Due an hour after it was read, and read a day ago: on time when systemd was asked.
READ_A_DAY_AGO = units(service(read_at=ago(days=1)), timer(read_at=ago(days=1), next_elapse_at=ago(hours=23)))
QUIET = (RUNNING, ACTIVE, NONE, READ_A_DAY_AGO)
# A mount a deploy never started, and a job of HQ's that requires it failed.
SHORT_DEPLOY = raising(
    failed(requires=["run-example.mount"]),
    socket("run-example.mount", activates="", active="inactive", sub="dead"),
    raises=(
        Raised(
            title="run-example.mount is not set up on example-controller",
            beneath=(f"{SERVICE} failed on example-controller",),
        ),
    ),
)

EXAMPLES = (
    Examples(
        "unit-failed",
        fires=(
            # A failed service nothing starts again is serious, and says how it ended.
            raising(failed(ended_at=ago(hours=5)), raises=(f"{SERVICE} failed on example-controller",)),
            # A failed job its timer starts again is attention.
            raising(
                failed(),
                timer(),
                raises=(Raised(title=f"{SERVICE} failed on example-controller", severity=Tone.ATTENTION),),
            ),
            # One service among scheduled jobs makes it serious.
            raising(
                failed(),
                timer(),
                failed("severino-hq-example-web.service"),
                raises=("2 HQ units failed on example-controller",),
            ),
            # Every failed unit on a machine is one finding.
            raising(
                failed(),
                failed(TIMER, result="resources"),
                failed("severino-hq-example-job@example.work.service"),
                raises=("3 HQ units failed on example-controller",),
            ),
            # A looping service that failed is a failed one, called by its title.
            raising(
                looping(active="failed", sub="failed", result="start-limit-hit"),
                raises=("“Example upload” failed on example-controller",),
            ),
            # A title holds whatever its unit file says, a "|" included.
            raising(
                failed(title="Example | nightly"),
                raises=("“Example | nightly” failed on example-controller",),
            ),
            # A record the schema refused leaves the units that were read judged.
            raising(
                failed(),
                error="1 record did not match the reading's schema",
                raises=(f"{SERVICE} failed on example-controller",),
            ),
            # A renderer that says why it failed is its own finding, so its unit is left out.
            Example(
                readings={KIND: (failed(SECRETS), failed()), RENDERED: (read(outcome="failed", failure="host"),)},
                raises=(f"{SERVICE} failed on example-controller",),
            ),
            # Where a deploy left a unit it requires not set up, it is that unit's item.
            Example(readings=SHORT_DEPLOY.readings, raises=(f"{SERVICE} failed on example-controller",)),
            # What HQ's own program told systemd as it failed is why.
            raising(
                failed(status="the example image is not on this host: `hq-host deploy` pulls it"),
                raises=(
                    Raised(
                        title=f"{SERVICE} failed on example-controller",
                        explanation="The example image is not on this host: `hq-host deploy` pulls it.",
                    ),
                ),
            ),
            # One that failed before it could say so is its unit's to report.
            Example(
                readings={KIND: (failed(SECRETS),), RENDERED: (read(),)},
                raises=(f"{SECRETS} failed on example-controller",),
            ),
        ),
        quiet=QUIET,
    ),
    Examples(
        "unit-not-installed",
        fires=(
            # Each way a shipped unit is not running is said.
            raising(
                service(),
                {
                    "unit": "severino-hq-example-gone.service",
                    "load": "not-found",
                    "active": "inactive",
                    "sub": "dead",
                    "read_at": NOW,
                },
                service("severino-hq-example-masked.service", load="masked", file_state="masked"),
                timer("severino-hq-example-off.timer", file_state="disabled", active="inactive", sub="dead"),
                timer("severino-hq-example-idle.timer", active="inactive", sub="dead"),
                {**timer("severino-hq-example-idle.path"), "sub": "waiting", "active": "inactive"},
                timer(),
                raises=("5 HQ units are not set up on example-controller",),
            ),
            # A unit HQ declares is turned on by its verbs, pressed.
            Example(
                readings={
                    KIND: (
                        timer("severino-hq-example-off.timer", file_state="disabled", active="inactive", sub="dead"),
                    )
                },
                given=lambda: declare("example-off-timer", KIND, {"unit": "severino-hq-example-off.timer"}),
                raises=(
                    Raised(
                        title="severino-hq-example-off.timer is not set up on example-controller",
                        presses=("infrastructure.resource.enable", "infrastructure.resource.start"),
                    ),
                ),
            ),
            # A socket not listening and a mount not mounted are not started.
            raising(
                service(),
                socket("severino-hq-example.socket", active="inactive", sub="dead"),
                socket("run-example.mount", activates="", active="inactive", sub="dead"),
                raises=("2 HQ units are not set up on example-controller",),
            ),
            # A failed job of HQ's follows from a deploy that stopped short of it.
            SHORT_DEPLOY,
            # One that fails without requiring it is a matter of its own.
            raising(
                failed(),
                socket("run-example.mount", activates="", active="inactive", sub="dead"),
                raises=("run-example.mount is not set up on example-controller",),
            ),
            # A shipped service switched off is said even while it runs.
            raising(
                looping(SERVICE, foreign=False, active="active", sub="running", active_at=ago(days=3)),
                raises=("“Example upload” is not set up on example-controller",),
            ),
        ),
        quiet=(
            *QUIET,
            # A service that is not running now is not absent.
            units(service(), service("severino-hq-example-other.service", file_state="")),
            # A failed timer is failed, and not also absent.
            units(failed(TIMER, file_state="enabled")),
            # A disabled service HQ does not ship is not called not set up.
            units(looping(active="active", sub="running", active_at=ago(days=3))),
        ),
    ),
    Examples(
        "timer-stalled",
        fires=(
            raising(service(), timer(sub="elapsed", next_elapse_at=""), raises=(STALLED,)),
            # Behind its own next elapse when it was read.
            raising(service(), timer(next_elapse_at=ago(hours=2, minutes=1)), raises=(STALLED,)),
            raising(service(condition="no", started_at="", ended_at=""), timer(), raises=(STALLED,)),
            # A run that never ends.
            raising(
                service(active="activating", sub="start", started_at=ago(hours=3)),
                timer(sub="running", next_elapse_at=""),
                raises=(STALLED,),
            ),
        ),
        quiet=(
            *QUIET,
            # Within its grace of its next elapse.
            units(service(), timer(next_elapse_at=ago(hours=1, minutes=59))),
            # A unit not started since boot was not skipped: systemd says no with no time.
            units(
                service(condition="no", condition_at="", started_at="", ended_at="", main_code=0),
                timer(last_trigger_at=""),
            ),
            # A monotonic schedule has no next elapse and is not late.
            units(service(), timer(next_elapse_at=""), timer("example-b.timer", sub="running", next_elapse_at="")),
            # A run an hour into its start.
            units(
                service(active="activating", sub="start", started_at=ago(hours=1)),
                timer(sub="running", next_elapse_at=""),
            ),
            # A timer switched off or not started is not set up, not stalled.
            units(
                timer("severino-hq-example-off.timer", file_state="disabled", active="inactive", sub="dead"),
                timer("severino-hq-example-idle.timer", active="inactive", sub="dead"),
            ),
        ),
    ),
    Examples(
        "unit-restarting",
        fires=(
            raising(service(), looping(), raises=(LOOPING,)),
            # Up only minutes since its last restart, it is still looping.
            raising(looping(active="active", sub="running", active_at=ago(minutes=2)), raises=(LOOPING,)),
            # Up minutes is measured from the reading, not from now.
            raising(
                looping(active="active", sub="running", active_at=ago(minutes=62), read_at=ago(hours=1)),
                raises=(LOOPING,),
            ),
        ),
        quiet=(
            *QUIET,
            # Restarted, then stayed up.
            units(looping(active="active", sub="running", active_at=ago(days=3))),
            # A shipped service restarted a few times is not a loop.
            units(looping(SERVICE, restarts=3, foreign=False)),
            # A looping service that failed is a failed one.
            units(looping(active="failed", sub="failed", result="start-limit-hit")),
        ),
    ),
    Examples(
        "unit-state-unread",
        fires=(
            # A reading the controller could not take, said never in its words.
            Example(readings={KIND: stored(reachable=False, error="no unit state was mounted")}, raises=(UNREAD,)),
            # A record the schema refused is said beside the units that were read.
            raising(failed(), error="1 record did not match the reading's schema", raises=(UNREAD,)),
        ),
        quiet=QUIET,
    ),
)

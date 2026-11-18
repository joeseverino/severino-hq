from datetime import timedelta

from ...tone import Tone
from . import Ago, Example, Examples, Raised, ago, stored

KIND = "host.render_status"
# Past what each threshold allows (render_status_findings.CONFIRMED_WITHIN, RENDERED_WITHIN).
CONFIRMED_WITHIN = timedelta(hours=3, seconds=90)
RENDERED_WITHIN = timedelta(hours=27, seconds=90)
MINUTE = timedelta(minutes=1)


def document(
    *,
    outcome: str = "current",
    failure: str = "",
    attempted: timedelta = timedelta(minutes=10),
    confirmed: timedelta | None = timedelta(minutes=10),
    rendered: timedelta = timedelta(hours=5),
    sync: str | None = "ACTIVE",
) -> dict:
    """A status document as ``secretstatus.Status`` marshals it."""

    found: dict = {
        "schema_version": 1,
        "last_attempt": {"at": Ago(attempted), "outcome": outcome} | ({"failure": failure} if failure else {}),
    }
    if confirmed is not None:
        found["last_success"] = {
            "at": Ago(confirmed),
            "rendered_at": Ago(max(rendered, confirmed)),
            "content_version": 42,
            "attribute_version": 3,
            "counts": {"items_read": 20, "connections": 9, "app_variables": 31, "identities": 2, "signing_keys": 1},
        }
    if sync is not None:
        found["connect"] = {
            "read_at": Ago(attempted),
            "version": "1.8.1",
            "dependencies": [{"service": "sqlite", "status": "ACTIVE"}, {"service": "sync", "status": sync}],
        }
    return found


def read(renderer: str = "hq", **status) -> dict:
    return {"renderer": renderer, "state": "read", "status": document(**status)}


def unread(reason: str, renderer: str = "hq") -> dict:
    return {"renderer": renderer, "state": "unreadable", "reason": reason}


def rendering(*records: dict, raises: tuple[Raised | str, ...] = (), **fields) -> Example:
    return Example(readings={KIND: stored(*records, **fields)}, raises=raises)


QUIET = (
    rendering(read()),
    # A full render just now.
    rendering(read(outcome="rendered", attempted=timedelta(0), confirmed=timedelta(0), rendered=timedelta(0))),
    # A reading no launcher names.
    rendering(connected=False),
    # No reading.
    Example(),
)
STALE = CONFIRMED_WITHIN + MINUTE
REFUSED = "example-controller could not refresh its credentials from 1Password: "
UNTOLD = "HQ cannot tell whether example-controller's credentials are fresh"


def failing(reason: str, severity: Tone | None = Tone.ATTENTION) -> Raised:
    return Raised(title=REFUSED + reason, severity=severity)


SYNCED = "1Password Connect on example-controller has stopped syncing"


EXAMPLES = (
    Examples(
        "render-failing",
        fires=(
            # One failed run on files still inside the allowance: its class and the commands.
            rendering(
                read(outcome="failed", failure="connect_denied", confirmed=timedelta(hours=1, minutes=10)),
                raises=(failing("1Password Connect refused the token"),),
            ),
            # A failure that outlasts the allowance is serious.
            rendering(
                read(
                    outcome="failed",
                    failure="connect_unavailable",
                    confirmed=timedelta(days=8),
                    rendered=timedelta(days=8),
                ),
                raises=(failing("1Password Connect did not answer in time, or the vault kept changing", None),),
            ),
            # A renderer that never succeeded is serious.
            rendering(
                read(outcome="failed", failure="config", confirmed=None, sync=None),
                raises=(failing("the job's configuration was refused", None),),
            ),
            # A class this release does not know is still named.
            rendering(
                read(outcome="failed", failure="example_class"),
                raises=(failing("an error HQ does not recognize (example_class)"),),
            ),
            # A renderer with no known unit gets its steps in words.
            rendering(
                read("other-apps", outcome="failed", failure="content"),
                raises=(Raised(title=REFUSED + "something in the vault was refused", severity=Tone.ATTENTION),),
            ),
            # Two renderers on one machine are one finding naming both.
            rendering(
                read(outcome="failed", failure="content"),
                read("other-apps", outcome="failed", failure="busy"),
                raises=(
                    Raised(
                        title=REFUSED + "something in the vault was refused, another refresh was already running",
                        severity=Tone.ATTENTION,
                    ),
                ),
            ),
        ),
        quiet=(
            *QUIET,
            # Stopped running is stale, not failing.
            rendering(read(attempted=STALE, confirmed=STALE, rendered=STALE)),
            # A sync that stalled while the render succeeded from Connect's cache.
            rendering(read(sync="TOKEN_NEEDED")),
        ),
    ),
    Examples(
        "render-stale",
        fires=(
            # A renderer that stopped running is serious.
            rendering(
                read(attempted=STALE, confirmed=STALE, rendered=STALE),
                raises=("example-controller has not refreshed its credentials since Jan\xa015, 2:57\xa0AM",),
            ),
            # A full read overdue is stale while every run says current.
            rendering(
                read(rendered=RENDERED_WITHIN + MINUTE),
                raises=("example-controller has not read its credentials in full since Jan\xa014, 2:57\xa0AM",),
            ),
            # Behind when it was read, it is still behind hours later.
            rendering(
                read(
                    attempted=timedelta(hours=2) + STALE,
                    confirmed=timedelta(hours=2) + STALE,
                    rendered=timedelta(hours=2) + STALE,
                ),
                age=timedelta(hours=2),
                raises=("example-controller has not refreshed its credentials since Jan\xa015, 12:57\xa0AM",),
            ),
        ),
        quiet=(
            *QUIET,
            # Two runs missed is not yet stale.
            rendering(read(attempted=CONFIRMED_WITHIN - MINUTE, confirmed=CONFIRMED_WITHIN - MINUTE)),
            # A full read a day old is not yet overdue.
            rendering(read(rendered=RENDERED_WITHIN - MINUTE)),
            # A failed run is failing, not stale, however long it outlasts the allowance.
            rendering(read(outcome="failed", failure="connect_denied", confirmed=timedelta(hours=1, minutes=10))),
            rendering(
                read(
                    outcome="failed",
                    failure="connect_unavailable",
                    confirmed=timedelta(days=8),
                    rendered=timedelta(days=8),
                )
            ),
            # Taken hours ago while nobody looked: current when read, with every hour since to run again.
            rendering(
                read(
                    attempted=CONFIRMED_WITHIN + timedelta(hours=2),
                    confirmed=CONFIRMED_WITHIN + timedelta(hours=2),
                    rendered=CONFIRMED_WITHIN + timedelta(hours=2),
                ),
                age=CONFIRMED_WITHIN + timedelta(hours=2),
            ),
            # A reading the controller stopped taking is unread, not stale.
            rendering(
                read(attempted=timedelta(days=1), confirmed=timedelta(days=1), rendered=timedelta(days=1)),
                age=timedelta(days=1),
            ),
            rendering(read(sync="TOKEN_NEEDED")),
        ),
    ),
    Examples(
        "connect-sync-stalled",
        fires=(
            rendering(read(sync="TOKEN_NEEDED"), raises=(SYNCED,)),
            # A Connect that reports no sync dependency is not vouched for.
            rendering(
                {
                    **read(),
                    "status": {
                        **document(),
                        "connect": {
                            "read_at": ago(minutes=10),
                            "version": "1.8.1",
                            "dependencies": [{"service": "sqlite", "status": "ACTIVE"}],
                        },
                    },
                },
                raises=(SYNCED,),
            ),
        ),
        quiet=(
            *QUIET,
            # A run that never reached Connect says nothing of sync.
            rendering(read(sync=None)),
            rendering(read(outcome="failed", failure="config", confirmed=None, sync=None)),
        ),
    ),
    Examples(
        "render-status-unread",
        fires=(
            # A missing document names the renderer.
            rendering(unread("missing"), raises=(Raised(title=UNTOLD),)),
            # One unread beside one healthy names only the unread.
            rendering(read(), unread("invalid", "other-apps"), raises=(Raised(title=UNTOLD),)),
            # A record the schema refused is a finding, not a silence.
            rendering(error="1 record did not match the reading's schema", raises=(Raised(title=UNTOLD),)),
            # A reading the controller could not take.
            rendering(
                reachable=False, error="the render status list is not name=path pairs", raises=(Raised(title=UNTOLD),)
            ),
            # A reading the controller stopped taking says no more whether the job runs.
            rendering(
                read(attempted=timedelta(days=1), confirmed=timedelta(days=1), rendered=timedelta(days=1)),
                age=timedelta(days=1),
                raises=(
                    Raised(
                        title="HQ cannot tell whether example-controller's credentials are fresh",
                        presses=("infrastructure.controller.refresh",),
                    ),
                ),
            ),
        ),
        quiet=QUIET,
    ),
)

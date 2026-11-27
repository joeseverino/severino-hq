"""How old a reading may be before it is out of date, per kind.

One answer for every surface, and nothing else decides it. A reading is *due*
once one cadence of its kind has passed, or when none was ever taken, so a
surface may ask for it, and *stale* once the tolerance has passed with no
newer reading, so a surface says it is out of date.

A kind read only when asked (``on_open``: its page opening, or HQ applying
it) ages while nobody asks, which is no fault. Its tolerance counts from an ask
instead: it is stale once an ask went unanswered that long, or its last reading
failed.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cache

lazy from django.core.exceptions import ImproperlyConfigured

lazy from hq.domains.control_plane.observations import OBSERVATIONS
lazy from hq.domains.control_plane.providers import PROVIDERS, provider_reads
lazy from hq.platform.core.included_parts import READING_AGE

from .derivations import reached
from .labels import dotted
from .moments import duration, when
from .process_caches import kept
lazy from . import plugins
lazy from .cadence import slowest_sweep_interval, ssh_probe_interval
lazy from .tables import Piece

CURRENT = "current"
DUE = "due"
STALE = "stale"
NEVER = "never"

# What every page calls the moment its reading was taken.
LAST_READ = "Last read"
# What every page calls a reading past its kind's tolerance.
OUT_OF_DATE = "Out of date"


def last_read(at: datetime | str) -> str:
    """When a reading was taken, as a sentence in plain text."""

    return f"{LAST_READ} {when(at)}"


def reading_age(reading: Freshness | None = None, *, at: datetime | None = None, unread: str = "") -> Piece:
    """A reading and its age as a table's cell draws it, in the one wording
    every page uses (``partials/_reading_age.html``), given as it takes them."""

    if reading is not None:
        stale = reading.observed_at and reading.stale
        words = dotted(reading.last_read, f"{reading.label.lower()}{reading.cadence}" if stale else "")
    else:
        words = last_read(at) if at else unread or "No reading yet"
    return Piece(READING_AGE, {"reading": reading, "at": at, "unread": unread}, words)


# Cadence names for readings that are not observation kinds.
DASHBOARD_GLANCE = "dashboard.glance"
SSH_PROBE = "connection.ssh_probe"
PAGE_VISIT = "page.visit"
FIRST_SEEN = "attention.first-seen"
AVATAR = "auth.avatar"

# A dashboard reading costs a trip to a machine, so it is re-read when the
# dashboard is opened and older than this.
GLANCE_EVERY = timedelta(minutes=5)

# What a page about one thing is assembled from is re-read when that page is
# opened and older than this. A provider API call, not a trip to a machine, so
# it may be asked sooner than a dashboard reading; an SSH connection keeps its
# own, slower, cadence however the page is opened.
VISIT_EVERY = timedelta(minutes=2)

# How long a reading asked for when a page opens may take to arrive. One takes
# seconds, so an ask still unanswered after this one never reached a reader.
ANSWER_WITHIN = timedelta(minutes=1)

# A read or hand-off that failed is asked again after this, not on every
# pass, whatever the cadence of its kind. GitHub's allowance is by the hour,
# so an hour also lets a read refused by its rate limit succeed.
RETRY_AFTER = timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class ReadCadence:
    """How often a kind is read, and how old a reading may get before it is stale.

    An extension declares its kinds' in ``PluginIntegration.cadences``.
    """

    every: timedelta
    stale_after: timedelta
    # Read only when a page opens, so ``stale_after`` counts from an
    # unanswered ask, never from the reading.
    on_open: bool = False

    def __post_init__(self) -> None:
        if self.every <= timedelta(0) or self.stale_after <= timedelta(0):
            raise ValueError("A read cadence's every and stale_after are positive.")


def missed(every: timedelta, runs: int) -> ReadCadence:
    """The cadence of a reading taken ``every`` so often, stale once ``runs``
    of them have gone by with none newer."""

    return ReadCadence(every, every * runs)


def on_open(every: timedelta) -> ReadCadence:
    """The cadence of a reading asked for when its page opens and it is older
    than ``every``, out of date only when an ask goes unanswered."""

    return ReadCadence(every, ANSWER_WITHIN, on_open=True)


# The cadences the host names itself, each read as its kind is judged; an
# observation kind is the host's too.
_NAMED: Mapping[str, Callable[[], ReadCadence]] = {
    DASHBOARD_GLANCE: lambda: on_open(GLANCE_EVERY),
    PAGE_VISIT: lambda: on_open(VISIT_EVERY),
    SSH_PROBE: lambda: missed(ssh_probe_interval(), 2),
    # A look at what is open derives the whole queue, so a report is followed
    # by one at most this often. A new item is dated by a look only while the
    # look before it is current; past that it began somewhere in a gap.
    FIRST_SEEN: lambda: missed(timedelta(minutes=15), 4),
    # A session is renewed every few minutes and each renewal is a sign-in, so
    # a picture is asked for again only once it is a day old.
    AVATAR: lambda: missed(timedelta(days=1), 2),
}


def host_kind(kind: str) -> bool:
    """Whether the host declares ``kind``'s cadence, which no extension may."""

    return kind in _NAMED or kind in OBSERVATIONS


@kept
def _extension_cadences(_composition: plugins.Composition) -> dict[str, Callable[[], object]]:
    """Each extension kind's cadence, refused whole when a kind is the host's,
    is declared twice, or is answered by no callable."""

    found: dict[str, Callable[[], object]] = {}
    owner: dict[str, str] = {}
    refused: list[str] = []
    for plugin, integration in plugins.installed_integrations():
        if integration.cadences is None:
            continue
        # What an extension returned, which its annotation does not hold it to.
        answers: Mapping[str, object] = integration.cadences()
        for kind, declared in answers.items():
            if not kind.strip():
                refused.append(f"{plugin.id} declares a cadence for no kind")
            elif not callable(declared):
                refused.append(f"{plugin.id} declares {kind!r} with no callable")
            elif host_kind(kind):
                refused.append(f"{plugin.id} declares {kind!r}, whose cadence the host declares")
            elif kind in owner:
                refused.append(f"{plugin.id} and {owner[kind]} both declare {kind!r}")
            else:
                owner[kind] = plugin.id
                found[kind] = declared
    if refused:
        raise ImproperlyConfigured(f"Extension read cadences refused: {'; '.join(refused)}.")
    return found


def _extension_cadence(kind: str) -> ReadCadence | None:
    declared = _extension_cadences(plugins.composition()).get(kind)
    if declared is None:
        return None
    found = declared()
    if not isinstance(found, ReadCadence):
        raise ImproperlyConfigured(f"The cadence declared for {kind!r} is no ReadCadence: {found!r}.")
    return found


def cadence(kind: str = "") -> ReadCadence:
    """The cadence one kind of reading is taken on."""

    if (named := _NAMED.get(kind)) is not None:
        return named()
    spec = OBSERVATIONS.get(kind)
    if spec is not None and spec.every is not None:
        return missed(spec.every, 2)
    if spec is None and (read := _read_through().get(kind)):
        # A connection HQ reaches itself is judged by the readings through it:
        # by the slowest it reads on a clock, else by one asked for on open.
        return max((cadence(item) for item in read), key=lambda found: (not found.on_open, found.stale_after))
    if spec is None and kind and (declared := _extension_cadence(kind)) is not None:
        return declared
    if (provider := PROVIDERS.get(kind)) is not None and not provider.sweeps:
        # No sweep reads it, so what the controller applied is its reading.
        if (every := provider.rechecked_every) is None:
            return on_open(slowest_sweep_interval())
        # Read again at the first pass after it falls due, which may be a
        # whole sweep's tolerance away.
        return ReadCadence(every, every + stale_after())
    # A controller reads on the sweep, whose slowest interval is the idle one;
    # a reading is stale once it has missed a whole sweep, not when one is due.
    return missed(slowest_sweep_interval(), 2)


@cache
def _read_through() -> dict[str, tuple[str, ...]]:
    """The kinds of reading HQ takes itself, by the connection they come through."""

    return {
        provider: kinds
        for provider, reads in provider_reads().items()
        if (kinds := tuple(read.kind for read in reads if read.by == "hq"))
    }


def stale_after(kind: str = "") -> timedelta:
    return cadence(kind).stale_after


@dataclass(frozen=True, slots=True)
class Freshness:
    state: str
    observed_at: datetime | None
    # Its kind's cadence: how often it is read, and how old it may get.
    read: ReadCadence
    # An ask for a reading taken on open went unanswered, so asking again is due.
    unanswered: bool = False

    @property
    def every(self) -> timedelta:
        return self.read.every

    @property
    def stale_after(self) -> timedelta:
        return self.read.stale_after

    @property
    def due(self) -> bool:
        # Asking is how the first reading arrives, so none yet is due.
        return self.state in (DUE, STALE, NEVER)

    @property
    def stale(self) -> bool:
        return self.state == STALE

    @property
    def last_read(self) -> str:
        """When the reading was taken, as a sentence; its label when none was."""

        return last_read(self.observed_at) if self.observed_at else self.label

    @property
    def cadence(self) -> str:
        """How often its kind is read, as the words an out-of-date age ends with."""

        # One of a unit is said as the unit alone; the count and its unit hold on a non-breaking space.
        return f"; read every {duration(self.every).removeprefix('1\xa0')}"

    @property
    def label(self) -> str:
        return {
            CURRENT: "Current",
            DUE: "Current",
            STALE: OUT_OF_DATE,
            NEVER: "No reading yet",
        }[self.state]


def freshness(
    kind: str,
    observed_at: datetime | None,
    now: datetime | None = None,
    *,
    asked_at: datetime | None = None,
    failed: bool = False,
) -> Freshness:
    """Whether a reading of ``kind`` taken at ``observed_at`` is current.

    ``asked_at`` is when a reading still unanswered was asked for, and
    ``failed`` whether the last one failed; only a kind read on open is judged
    by them.
    """

    found = cadence(kind)
    unanswered = found.on_open and asked_at is not None and reached(asked_at + found.stale_after, now=now)
    if observed_at is None:
        return Freshness(NEVER, None, found, unanswered)
    if (failed or unanswered) if found.on_open else reached(observed_at + found.stale_after, now=now):
        state = STALE
    elif reached(observed_at + found.every, now=now):
        state = DUE
    else:
        state = CURRENT
    return Freshness(state, observed_at, found, unanswered)


# How far behind its own kind's newest reading a record may fall before the
# gap means a sweep passed over it rather than reached it a moment later.
# Sweeps write one timestamp for everything they confirm, so siblings land
# together, and a kind on a slower clock is slower, never behind.
_BEHIND = timedelta(hours=1)


def behind(observed: datetime | None, newest: datetime | None) -> bool:
    """Whether a record last seen at ``observed`` was left behind by its
    kind's newest reading, taken at ``newest``."""

    return observed is not None and newest is not None and newest - observed > _BEHIND

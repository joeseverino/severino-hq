"""Each open Renovate pull request that moves an app's Compose pins.

Read from the GitHub App's ``github.repository`` reading, which keeps each
update's files and the diff of each Compose file (``github_readings.go``). The
app is the directory under ``compose/`` a file sits in; each ``image:`` line
the diff takes out and puts in is a pin moved. A pin joins to the containers
it moves by the compose file that defines them and their image, and the move
is judged by the one judge every upgrade uses (``upgrades.weigh``). Every
page, the API and MCP read ``app_updates()``.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .containers import Container, containers, published_digest, ranked, standing_of
from .github_estate import UPDATE_BRANCH, pull_checks, repositories
from .images import ImageRef
from .projection import projection_scope, read_once
from .tone import Tone
from .ui import counted
from .upgrades import weigh

# The directory each app's Compose files sit under, one directory per app: the
# only paths whose diff the reading keeps.
APPS = "compose/"
# An ``image:`` line, its reference unquoted and without a comment, and a
# line that opens a mapping (``  web:``), which an ``image:`` below it belongs to.
_IMAGE = re.compile(r"""^\s*image:\s*["']?(?P<reference>[^"'\s#]+)""")
_KEY = re.compile(r"^\s*(?P<key>[\w.-]+):\s*(?:#.*)?$")


@dataclass(frozen=True, slots=True)
class Pin:
    """One ``image:`` line a pull request moves."""

    # The compose service it is under, or "" where the diff's lines do not say.
    service: str
    # None where the line is new.
    before: ImageRef | None
    after: ImageRef
    # The digest the new pin names, as written or as its registry says its
    # tag names: what a scan reads. "" while neither says.
    digest: str
    # Its packages checked against OSV, None until they are.
    checked: Mapping[str, Any] | None

    @property
    def scanned(self) -> bool:
        return self.checked is not None

    @property
    def said(self) -> str:
        """``v1.2.0 to v1.2.1``, or the new tag alone where the line is new."""

        return f"{self.before.tag or self.before.short} to {self.after.tag}" if self.before else self.after.tag


@dataclass(frozen=True, slots=True)
class AppUpdate:
    """One open update of one app, and what it would change where it runs."""

    repository: str
    number: int
    url: str
    title: str
    # The pull request's head commit: what an approval is of.
    head: str
    app: str
    pins: tuple[Pin, ...]
    containers: tuple[Container, ...]
    # Of what makes each moved container vulnerable, what its new pin is
    # clear of; and what the new pins bring of their own. Worst first.
    fixes: tuple[Mapping[str, Any], ...]
    brings: tuple[Mapping[str, Any], ...]
    checks: Mapping[str, Any]
    # Whether GitHub gave the diff of every Compose file it changes: a large
    # diff comes without one, and its pins are read on GitHub alone.
    readable: bool
    patch: str

    @property
    def label(self) -> str:
        return f"#{self.number}"

    @property
    def scanned(self) -> bool:
        """Whether every new pin's packages were checked: until then, what it
        brings is not known."""

        return self.readable and all(pin.scanned for pin in self.pins)

    @property
    def checks_said(self) -> tuple[str, str, str]:
        """Its checks as every page says them: badge, tone, the failing ones."""

        return pull_checks({"checks": self.checks})

    @property
    def fixes_tone(self) -> str:
        return Tone.GOOD if self.fixes else ""

    @property
    def brings_tone(self) -> str:
        if self.brings:
            return Tone.SERIOUS
        return Tone.GOOD if self.scanned else Tone.ATTENTION

    @property
    def fixes_said(self) -> str:
        return counted(len(self.fixes), "known vulnerability", "known vulnerabilities") if self.fixes else "None known"

    @property
    def fixes_counted(self) -> str:
        """What it fixes as a row's detail says it: ``fixes 2``, or ""."""

        return f"fixes {len(self.fixes)}" if self.fixes else ""

    @property
    def brings_said(self) -> str:
        if self.brings:
            return counted(len(self.brings), "serious vulnerability", "serious vulnerabilities")
        return "Nothing serious" if self.scanned else "Not scanned yet"

    @property
    def evidence(self) -> tuple[tuple[str, str], ...]:
        """The update as a card's evidence: where it is open, each pin's move,
        and what it fixes and brings."""

        return (
            ("Pull request", f"{self.repository}{self.label}"),
            *(("Pin", pin.said) for pin in self.pins),
            ("Fixes", self.fixes_said),
            ("Brings", self.brings_said),
        )

    def moves(self, item: Container) -> bool:
        return any(each.address == item.address for each in self.containers)

    def pin_of(self, item: Container) -> Pin | None:
        """The pin that moves ``item``, where the diff says which."""

        return _pin_of(item, self.pins)


def app_updates() -> tuple[AppUpdate, ...]:
    """Every open update of an app, by repository and then newest first."""

    with projection_scope():
        return read_once("app_updates.app_updates", _app_updates)


def update_of(item: Container) -> AppUpdate | None:
    """The open update that moves ``item``, the newest when more than one does."""

    return next((update for update in app_updates() if update.moves(item)), None)


def _app_updates() -> tuple[AppUpdate, ...]:
    running = containers()
    found = []
    for name, repository in sorted(repositories().items()):
        pulls = (
            pull for pull in repository.pull_requests or () if str(pull.get("branch") or "").startswith(UPDATE_BRANCH)
        )
        for pull in sorted(pulls, key=lambda pull: -int(pull.get("number") or 0)):
            for app, files in _by_app(pull.get("files") or ()).items():
                found.append(_update(name, pull, app, files, running))
    return tuple(found)


def _by_app(files: Iterable[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    """The files under each app's directory: ``compose/<app>/…``."""

    found: dict[str, list[Mapping[str, Any]]] = {}
    for item in files:
        app, _, rest = str(item.get("path") or "").removeprefix(APPS).partition("/")
        if str(item.get("path") or "").startswith(APPS) and app and rest:
            found.setdefault(app, []).append(item)
    return found


def _update(
    repository: str, pull: Mapping[str, Any], app: str, files: list[Mapping[str, Any]], running: list[Container]
) -> AppUpdate:
    readable = all(item.get("patch") for item in files)
    patch = "\n".join(str(item.get("patch") or "") for item in files if item.get("patch"))
    pins = tuple(_pin(service, before, after) for service, before, after in _moved(patch))
    moved = tuple(item for item in running if _defined_in(item, files) and (not pins or _pin_of(item, pins)))
    fixes: dict[str, Mapping[str, Any]] = {}
    brings: dict[tuple[str, str], Mapping[str, Any]] = {}
    for item in moved:
        pin = _pin_of(item, pins)
        if pin is None:
            continue
        target = standing_of(_pinned(pin))
        fixed, brought = weigh(item.standing, pin.after.tag, target.advisories, pin.checked)
        for each in fixed:
            fixes.setdefault(str(each.get("id", "")), each)
        for each in brought:
            brings.setdefault((str(each.get("id", "")), str(each.get("package", ""))), each)
    return AppUpdate(
        repository=repository,
        number=int(pull.get("number") or 0),
        url=str(pull.get("url") or ""),
        title=str(pull.get("title") or ""),
        head=str(pull.get("head") or ""),
        app=app,
        pins=pins,
        containers=moved,
        fixes=ranked(fixes.values()),
        brings=ranked(brings.values()),
        checks=pull.get("checks") or {},
        readable=readable,
        patch=patch,
    )


def _pin(service: str, before: ImageRef | None, after: ImageRef) -> Pin:
    digest = published_digest(after)
    checked = standing_of(f"{after.name}:{after.tag}@{digest}").checked if digest else None
    return Pin(service, before, after, digest, checked)


def _pinned(pin: Pin) -> str:
    return f"{pin.after.name}:{pin.after.tag}" + (f"@{pin.digest}" if pin.digest else "")


def _moved(patch: str) -> tuple[tuple[str, ImageRef | None, ImageRef], ...]:
    """Each ``image:`` line the diff puts in, with the service it is under and
    the line it takes out for it: the same service's, else the same image's."""

    taken: list[tuple[str, ImageRef]] = []
    put: list[tuple[str, ImageRef]] = []
    # The mappings open above the line, by indent: the nearest is its owner.
    above: list[tuple[int, str]] = []
    for line in patch.splitlines():
        mark, text = line[:1], line[1:]
        if line.startswith("@@"):
            above = []
        if mark not in (" ", "+", "-") or not text.strip():
            continue
        indent = len(text) - len(text.lstrip())
        above = [(depth, key) for depth, key in above if depth < indent]
        if found := _IMAGE.match(text):
            reference = found["reference"]
            image = None if "$" in reference else ImageRef.parse(reference)
            if image is not None and mark != " ":
                (put if mark == "+" else taken).append((above[-1][1] if above else "", image))
        elif found := _KEY.match(text):
            above.append((indent, found["key"]))
    moved = []
    for service, after in put:
        before = next(
            (image for owner, image in taken if (owner == service if owner and service else image.name == after.name)),
            None,
        )
        if before != after:
            moved.append((service, before, after))
    return tuple(moved)


def _defined_in(item: Container, files: list[Mapping[str, Any]]) -> bool:
    """Whether a compose file that defines ``item`` is one the update changes:
    the machine's path ends in the app's directory and the file's name."""

    ends = tuple("/" + str(each.get("path") or "").removeprefix(APPS) for each in files)
    return any(path.endswith(ends) for path in item.compose_files)


def _pin_of(item: Container, pins: tuple[Pin, ...]) -> Pin | None:
    """The pin that moves ``item``: of its image, and of its service where both say one."""

    service = str((item.runtime or {}).get("service", "") or "")
    name = item.standing.image.name
    for pin in pins:
        names = {pin.after.name, *((pin.before.name,) if pin.before else ())}
        if name in names and (not pin.service or not service or pin.service == service):
            return pin
    return None

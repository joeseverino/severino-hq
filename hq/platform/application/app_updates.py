"""Each open Renovate pull request that moves an app's Compose pins, and the
apps a repository holds whether or not one is open.

Read from the GitHub App's ``github.repository`` reading, which keeps, for a
repository opted in to updates, each app's Compose files and each update's
images before and after as compose-go loads them (``github_compose.go``). A
pin joins to the containers it moves by the compose file that defines them
and their image, and the move is judged by the one judge every upgrade uses
(``upgrades.weigh``). Every page, the API and MCP read ``app_updates()``.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .containers import Container, containers, published_digest, ranked, standing_of
from .github_estate import UPDATE_BRANCH, pull_checks, repositories
from .images import ImageRef
from .projection import projection_scope, read_once
from .tone import Tone
from .ui import counted
from .upgrades import weigh


@dataclass(frozen=True, slots=True)
class Pin:
    """One service's image a pull request moves."""

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
    # Whether its Compose files loaded at base and head with every image
    # resolved: otherwise its pins are read on GitHub alone.
    readable: bool

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
        """The pin that moves ``item``."""

        return _pin_of(item, self.pins)


def app_updates() -> tuple[AppUpdate, ...]:
    """Every open update of an app, by repository and then newest first."""

    with projection_scope():
        return read_once("app_updates.app_updates", _app_updates)


def update_of(item: Container) -> AppUpdate | None:
    """The open update that moves ``item``, the newest when more than one does."""

    return next((update for update in app_updates() if update.moves(item)), None)


@dataclass(frozen=True, slots=True)
class Managed:
    """The repository that holds a container's Compose files."""

    repository: str
    app: str
    # Its Dependency Dashboard, where Renovate lists an update it has not
    # opened yet, or "" where it keeps none.
    dashboard: str
    url: str


def managed_by(item: Container) -> Managed | None:
    """The repository whose Compose files define ``item``, whether or not an
    update of it is open."""

    with projection_scope():
        held = read_once("app_updates.managed", _managed)
    return next((held[end] for end in map(_tail, item.compose_files) if end in held), None)


def _managed() -> dict[str, Managed]:
    found: dict[str, Managed] = {}
    for name, repository in sorted(repositories().items()):
        for path in repository.compose:
            found.setdefault(_tail(path), Managed(name, _app(path), str(repository.dashboard), str(repository.url)))
    return found


def _tail(path: str) -> str:
    """A Compose file as its app's directory and its name, ``<app>/<file>``:
    where a repository's path and a machine's meet."""

    return "/".join(path.rsplit("/", 2)[-2:])


def _app(path: str) -> str:
    return _tail(path).partition("/")[0]


def _app_updates() -> tuple[AppUpdate, ...]:
    running = containers()
    found = []
    for name, repository in sorted(repositories().items()):
        pulls = (
            pull for pull in repository.pull_requests or () if str(pull.get("branch") or "").startswith(UPDATE_BRANCH)
        )
        for pull in sorted(pulls, key=lambda pull: -int(pull.get("number") or 0)):
            for change in pull.get("apps") or ():
                app = str(change.get("app") or "")
                files = {
                    _tail(path)
                    for path in (
                        *repository.compose,
                        *(str(each.get("path") or "") for each in pull.get("files") or ()),
                    )
                    if _app(path) == app
                }
                found.append(_update(name, pull, change, files, running))
    return tuple(found)


def _update(
    repository: str, pull: Mapping[str, Any], change: Mapping[str, Any], files: set[str], running: list[Container]
) -> AppUpdate:
    pins = tuple(
        _pin(
            str(image.get("service") or ""),
            ImageRef.parse(str(image["before"])) if image.get("before") else None,
            ImageRef.parse(str(image.get("after") or "")),
        )
        for image in change.get("images") or ()
    )
    moved = tuple(
        item
        for item in running
        if any(_tail(path) in files for path in item.compose_files) and (not pins or _pin_of(item, pins))
    )
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
        app=str(change.get("app") or ""),
        pins=pins,
        containers=moved,
        fixes=ranked(fixes.values()),
        brings=ranked(brings.values()),
        checks=pull.get("checks") or {},
        readable=bool(change.get("read")),
    )


def _pin(service: str, before: ImageRef | None, after: ImageRef) -> Pin:
    digest = published_digest(after)
    checked = standing_of(f"{after.name}:{after.tag}@{digest}").checked if digest else None
    return Pin(service, before, after, digest, checked)


def _pinned(pin: Pin) -> str:
    return f"{pin.after.name}:{pin.after.tag}" + (f"@{pin.digest}" if pin.digest else "")


def _pin_of(item: Container, pins: tuple[Pin, ...]) -> Pin | None:
    """The pin that moves ``item``: of its image, and of its service where both say one."""

    service = str((item.runtime or {}).get("service", "") or "")
    name = item.standing.image.name
    for pin in pins:
        names = {pin.after.name, *((pin.before.name,) if pin.before else ())}
        if name in names and (not pin.service or not service or pin.service == service):
            return pin
    return None

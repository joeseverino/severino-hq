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
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from django.utils.text import capfirst

from .card_items import Answer
from .container_words import named
from .containers import Container, containers, published_digest, standing_of
from .entity_links import EntityLink
from .github_estate import PullCheck, PullChecks, Repository, pull_checks, repositories
from .image_findings import ranked
from .images import ImageRef
from .projection import projection_scope, read_once
from .tone import Tone
from .ui import counted, ended
from .upgrades import fixes_said, plan_for, weigh
lazy from .app_update_approval import (
    APPLY,
    APPROVE,
    Approval,
    applied_at,
    apply_of,
    apply_target,
    approval_of,
    approve_target,
)
lazy from .app_update_flow import UPDATE
lazy from .app_update_open import OPEN, open_standing, open_target
lazy from .asks import IDLE, Standing
lazy from .container_findings import BY_HAND
lazy from .dependency_updates import last_run
lazy from .finding_model import Fact
lazy from .finding_steps import Does
lazy from .labels import dotted, lower_first
lazy from .pages import PageAction, command_reach
lazy from .sequences import PERSON, Sequence

if TYPE_CHECKING:
    from .dependency_updates import Branch

# What an update's new pins bring of their own, as every page labels it.
BRINGS = "New vulnerabilities"


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
    # What its publisher attached to that digest, None until read.
    attested: Mapping[str, Any] | None = None

    @property
    def scanned(self) -> bool:
        return self.checked is not None

    @property
    def unlisted(self) -> bool:
        """Whether its publisher attached no package list, so no scan can check it."""

        return self.attested is not None and not self.attested.get("packages")

    @property
    def said(self) -> str:
        """``v1.2.0 to v1.2.1``, or the new tag alone where the line is new."""

        return f"{self.before.said} to {self.after.tag}" if self.before else self.after.tag


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
    # What its update run said it could not update, while its checks fail.
    artifact_problem: tuple[str, ...]
    # Whether its Compose files loaded at base and head with every image
    # resolved: otherwise its pins are read on GitHub alone.
    readable: bool
    # Whether HQ's update run opened it from the repository itself: its author
    # is the login the run opens pull requests as, and its head branch is
    # the repository's own, never a fork's.
    renovates: bool
    # Whether every file it changes lies in the directory of an app it names.
    within_apps: bool
    # Whether HQ read every file GitHub says it changes.
    whole: bool
    # Each check on its head as its last run stands, or as GitHub told HQ
    # since (``Repository.pull_check_runs``).
    check_runs: tuple[PullCheck, ...] = ()

    @property
    def label(self) -> str:
        return f"#{self.number}"

    @property
    def scanned(self) -> bool:
        """Whether every new pin's packages were checked: until then, what it
        brings is not known. An update that moves no pin rebuilds on the host,
        from files no scan reads."""

        return self.readable and bool(self.pins) and all(pin.scanned for pin in self.pins)

    @property
    def checks_said(self) -> PullChecks:
        return pull_checks({"checks": self.checks, "artifact_problem": self.artifact_problem})

    @property
    def publisher(self) -> str:
        """Who publishes what it moves to, as its source repository names them, or ""."""

        return next((item.standing.upstream for item in self.containers if item.standing.upstream), "")

    @property
    def fixes_said(self) -> Answer:
        return fixes_said(self.fixes) if self.fixes else Answer("None known")

    @property
    def fixes_counted(self) -> str:
        """What it fixes as a row's detail says it: ``fixes 2``, or ""."""

        return f"fixes {len(self.fixes)}" if self.fixes else ""

    @property
    def brings_said(self) -> Answer:
        if self.brings:
            counts = counted(len(self.brings), "serious vulnerability", "serious vulnerabilities")
            return Answer(counts, named(self.brings), Tone.SERIOUS)
        if self.scanned:
            return Answer("Nothing serious", tone=Tone.GOOD)
        if self.readable and not self.pins:
            return Answer("Not checked", ("It rebuilds on the host",), Tone.ATTENTION)
        if any(pin.unlisted for pin in self.pins):
            publisher = self.publisher or "Its publisher"
            return Answer("Can't check", (f"{publisher} publishes no package list",), Tone.ATTENTION)
        return Answer("Not checked yet", ("Its new image's packages are not read yet",), Tone.ATTENTION)

    @property
    def evidence(self) -> tuple[Fact, ...]:
        """The update as a card's evidence: where it is open, each pin's move,
        and what it fixes and brings."""

        return (
            ("Pull request", EntityLink(f"{self.repository}{self.label}", self.url, external=True)),
            *(("Pin", pin.said) for pin in self.pins),
            ("Fixes", self.fixes_said.short),
            (BRINGS, self.brings_said.said),
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
            found.setdefault(_tail(path), Managed(name, _app(path), str(repository.url)))
    return found


def builds(files: tuple[str, ...], service: str) -> bool:
    """Whether a repository HQ reads declares ``service`` of the Compose files
    ``files`` as one that builds its image on its host."""

    with projection_scope():
        built = read_once("app_updates.builds", _builds)
    return any((_tail(path), service) in built for path in files)


def _builds() -> frozenset[tuple[str, str]]:
    return frozenset(
        (_tail(str(each.get("path") or "")), str(each.get("service") or ""))
        for repository in repositories().values()
        for each in repository.compose_images or ()
        if each.get("build")
    )


def _tail(path: str) -> str:
    """A Compose file as its app's directory and its name, ``<app>/<file>``:
    where a repository's path and a machine's meet."""

    return "/".join(path.rsplit("/", 2)[-2:])


def _app(path: str) -> str:
    return _tail(path).partition("/")[0]


def _app_updates() -> tuple[AppUpdate, ...]:
    running = containers()
    found = []
    for _name, repository in sorted(repositories().items()):
        checks = repository.pull_check_runs()
        for pull in sorted(repository.update_pulls, key=lambda pull: -int(pull.get("number") or 0)):
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
                found.append(_update(repository, pull, change, files, running, checks))
    return tuple(found)


def _update(
    repository: Repository,
    pull: Mapping[str, Any],
    change: Mapping[str, Any],
    files: set[str],
    running: list[Container],
    checks: Mapping[int, tuple[PullCheck, ...]],
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
    changed = pull.get("files") or ()
    named = {str(each.get("app") or "") for each in pull.get("apps") or ()}
    return AppUpdate(
        repository=repository.name,
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
        artifact_problem=tuple(pull.get("artifact_problem") or ()),
        check_runs=checks.get(int(pull.get("number") or 0), ()),
        readable=bool(change.get("read")),
        renovates=bool(repository.updater)
        and pull.get("author") == repository.updater
        and str(pull.get("head_repository") or "").casefold() == repository.name.casefold(),
        within_apps=all(each.get("app") in named for each in changed),
        whole=len(changed) == int(pull.get("changed_files") or 0),
    )


def _pin(service: str, before: ImageRef | None, after: ImageRef) -> Pin:
    digest = published_digest(after)
    target = standing_of(f"{after.name}:{after.tag}@{digest}") if digest else None
    return Pin(service, before, after, digest, target.checked if target else None, target.attested if target else None)


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


class UpdateKind(StrEnum):
    """Where an app's newer release stands on its way to where it runs."""

    # A pull request moves it.
    OPEN = "open"
    # Its repository's default branch pins it, and the container runs the pin before.
    MERGED = "merged"
    # Renovate holds its branch, with no pull request, as its last run's report says.
    HELD = "held"
    # Its repository holds it, and Renovate has opened nothing for the release.
    UNOPENED = "unopened"
    # A repository holds its app, but not the Compose file it runs from.
    UNMATCHED = "unmatched"
    # No repository HQ reads holds its Compose files: a person moves it.
    BY_HAND = "by_hand"


@dataclass(frozen=True, slots=True)
class UpdateAction:
    """The one press a state offers: its command, and the route arguments it posts to."""

    capability: str
    target: Mapping[str, str]

    @property
    def remedy(self) -> Does:
        url, _ = command_reach(self.capability, self.target)
        return Does(self.capability, url=url, method="POST")

    def control(self, back: str, refused: str = "") -> PageAction | None:
        return PageAction.command(self.capability, self.target, primary=True, query={"next": back}, refused=refused)


@dataclass(frozen=True, slots=True)
class UpdateState:
    """Where one container's update stands, and the one thing to do about it,
    as its finding, the queue and its page all say it (``update_state``)."""

    kind: UpdateKind
    # ``2.16.0 to 2.17.1``: from what runs to the pin the state knows, "" where none.
    move: str
    # The sentence beside the title.
    words: str
    # The press it stands on, offered while nothing holds it back.
    action: UpdateAction | None = None
    # Why the press is held back, "" when it is offered; and what the press does.
    reason: str = ""
    said: str = ""
    # Held because no one adopted playbook applies it: the Playbooks page's step.
    adopt: bool = False
    # How its pull request stands before Renovate opened it: its last Open
    # PR (``app_update_open.open_standing``), else what Renovate waits for.
    pull: Standing | None = None
    update: AppUpdate | None = None
    managed: Managed | None = None

    @property
    def offered(self) -> UpdateAction | None:
        """Its press, while nothing holds it back."""

        return None if self.reason else self.action

    @property
    def at_work(self) -> bool:
        """Whether HQ is moving it now, or moved it: nothing is asked of
        anyone. So it is once the person's steps are done, or while the one
        they pressed is under way."""

        flow, at = self.flow, self.flow.at
        return bool(flow.steps) and (flow.next is None or (at is not None and at.by == PERSON and at.live))

    @property
    def follows(self) -> bool:
        """Whether its card asks again: a step of its way, or a part of one, is under way."""

        return self.flow.live

    @property
    def flow(self) -> Sequence:
        """Its way to running, step by step (``app_update_flow.UPDATE``)."""

        return UPDATE.of(self)


UNOPENED = "Renovate has not seen the release yet."


def update_state(item: Container) -> UpdateState:
    """Where ``item``'s update stands: decided here alone, from the open pull
    request, its repository's default branch, Renovate's last run and the
    playbook that applies it, so no page and no finding decide it twice."""

    plan = plan_for(item)
    latest = f"{item.standing.tag} to {plan.target_tag}" if plan is not None else ""
    if (update := update_of(item)) is not None:
        pin = update.pin_of(item)
        fixes = f", and fixes {lower_first(update.fixes_said.short)}" if update.fixes else ""
        return _decided(
            UpdateKind.OPEN,
            pin.said if pin else latest,
            ended(f"Pull request {update.label} moves it{fixes}"),
            UpdateAction(APPROVE, approve_target(update)),
            approval_of(update),
            update=update,
        )
    managed = managed_by(item)
    if managed is None:
        if (unmatched := _unmatched(item)) is not None:
            holder, why = unmatched
            words = ended(f"HQ can't match it to the Compose files in {holder}")
            return UpdateState(UpdateKind.UNMATCHED, latest, words, reason=why)
        return UpdateState(UpdateKind.BY_HAND, latest, BY_HAND)
    repository = repositories()[managed.repository]
    if (move := _behind(item, repository, managed.app)) is not None:
        head = str((repository.head or {}).get("sha") or "")
        return _decided(
            UpdateKind.MERGED,
            move,
            ended(f"Merged in {managed.repository}, not applied"),
            UpdateAction(APPLY, apply_target(managed, head)),
            apply_of(managed.repository, head, (item,)),
            managed=managed,
        )
    action = UpdateAction(OPEN, open_target(managed.repository, managed.app))
    held = app_branch(managed.repository, managed.app)
    opening = open_standing(managed.repository, managed.app)
    if held is not None and held.held:
        said = ended(f"Asks Renovate to open {held.branch}")
        pull = opening or Standing(IDLE, dotted(capfirst(held.until), said), word="Held by Renovate")
        words = ended(f"Renovate is holding its branch {held.until}")
        return UpdateState(UpdateKind.HELD, latest, words, action, said=said, pull=pull, managed=managed)
    said = "Asks Renovate to run."
    pull = opening or Standing(IDLE, dotted(UNOPENED, said), word="Not opened")
    return UpdateState(UpdateKind.UNOPENED, latest, UNOPENED, action, said=said, pull=pull, managed=managed)


def _decided(
    kind: UpdateKind, move: str, words: str, action: UpdateAction, approval: Approval, **of: Any
) -> UpdateState:
    """A state whose press runs while ``approval`` holds nothing back."""

    held = {"reason": approval.held, "adopt": approval.adopt} if approval.held else {"said": approval.said}
    return UpdateState(kind, move, words, action, **held, **of)


def of_app(repository: str, app: str) -> tuple[Container, ...]:
    """The containers of ``app``, whose Compose files ``repository`` holds."""

    return tuple(
        item
        for item in containers()
        if (managed := managed_by(item)) is not None and (managed.repository, managed.app) == (repository, app)
    )


def merged_of(repository: str, app: str) -> tuple[Container, ...]:
    """The containers of ``app`` that run a pin before the one its repository's default branch holds."""

    return tuple(item for item in of_app(repository, app) if _behind(item, repositories()[repository], app) is not None)


def _behind(item: Container, repository: Any, app: str) -> str | None:
    """How ``item`` runs behind its repository's default branch, as its move
    says it: to the pin the head holds, or, for an image built on its host,
    from the revision its machine last released the app at to the head, when
    a commit since changed the app's directory; None where it runs what a
    merge brings."""

    if (pinned := _pinned_at_head(item, repository)) is not None:
        return f"{item.standing.tag or item.standing.image.short} to {pinned.tag}"
    build = next((each for each in repository.builds or () if each.get("app") == app), None)
    if not item.standing.built_here or build is None:
        return None
    applied = applied_at(item.machine.name, app)
    if not applied or applied in (build.get("commits") or ()):
        return None
    return f"{applied[:7]} to {str((repository.head or {}).get('sha') or '')[:7]}"


def _pinned_at_head(item: Container, repository: Any) -> ImageRef | None:
    """The image the default branch's head pins for ``item``, where it is not what runs."""

    tails = set(map(_tail, item.compose_files))
    service = str((item.runtime or {}).get("service", "") or "")
    standing = item.standing
    for each in repository.compose_images or ():
        pinned = ImageRef.parse(str(each.get("image") or ""))
        if (
            pinned is None
            or _tail(str(each.get("path") or "")) not in tails
            or pinned.name != standing.image.name
            or (service and each.get("service") and each["service"] != service)
        ):
            continue
        moved = pinned.tag != standing.tag or bool(
            pinned.digest and standing.digest and pinned.digest != standing.digest
        )
        return pinned if moved else None
    return None


def app_branch(repository: str, app: str) -> Branch | None:
    """The branch of ``app``'s update the last run of ``repository`` reported:
    the first whose upgrades change a file of the app's directory."""

    run = last_run(repository)
    return next((each for each in run.branches if any(_app(path) == app for path in each.files)), None) if run else None


def _unmatched(item: Container) -> tuple[str, str] | None:
    """The repository that holds ``item``'s app without the Compose file it
    runs from, and why HQ can't match the two; None where none holds it."""

    files = item.compose_files
    apps = {_app(path) for path in files} or {item.running.stack or item.running.name}
    for name, repository in sorted(repositories().items()):
        if held := next((path for path in repository.compose if _app(path) in apps), ""):
            if files:
                return name, ended(f"It holds {held}, and {item.machine.name} runs it from {files[0]}")
            return name, ended(f"HQ has not read the Compose files that run it, so it can't match them to {held}")
    return None

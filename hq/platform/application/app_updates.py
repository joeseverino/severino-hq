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

from hq.domains.control_plane.models import OperationRequest

from .card_items import Answer
from .containers import Container, containers, published_digest, ranked, standing_of
from .entity_links import EntityLink
from .github_estate import UPDATE_BRANCH, PullChecks, Repository, pull_checks, repositories
from .images import ImageRef
from .projection import projection_scope, read_once
from .tone import Tone
from .ui import counted, ended
from .upgrades import fixes_said, named, plan_for, weigh
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
lazy from .app_update_open import OPEN, open_standing, open_target
lazy from .asks import DONE, RUNNING, Asked, Standing
lazy from .container_findings import BY_HAND
lazy from .finding_model import Fact, FindingRemedy
lazy from .labels import lower_first
lazy from .pages import PageAction, command_reach
lazy from .resource_operations import OperationAsked, operation_asked
lazy from .work_runs import standing_of as run_standing

if TYPE_CHECKING:
    from .work_runs import WorkRun

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
                found.append(_update(repository, pull, change, files, running))
    return tuple(found)


def _update(
    repository: Repository,
    pull: Mapping[str, Any],
    change: Mapping[str, Any],
    files: set[str],
    running: list[Container],
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
    # Renovate holds its branch on the Dependency Dashboard, with no pull request.
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
    def remedy(self) -> FindingRemedy:
        url, _ = command_reach(self.capability, self.target)
        return FindingRemedy(capability=self.capability, target="", url=url, method="POST")

    def control(self, back: str) -> PageAction | None:
        return PageAction.command(self.capability, self.target, primary=True, query={"next": back})


@dataclass(frozen=True, slots=True)
class MergeStanding:
    """How an approved update stands: its preview, its merge, and the run
    that applies it."""

    # Where it stands in a word or two ("Waiting", "Apply failed").
    word: str
    tone: str
    # Whether it previews, merges, applies or merged, so no second approval is offered.
    taken: bool = False
    # Its fact's detail: what it waits on, or of a preview or an apply that
    # ended, the step it stopped on, what that said and each app the run did
    # not release.
    lines: tuple[str, ...] = ()
    # The run or reconcile it stands on; None for Renovate's run, which is a
    # read asked of the controller rather than an operation.
    operation: OperationRequest | None = None
    # That operation as every list of work shows it, whose page and standing
    # each surface that names it shows.
    run: WorkRun | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "lines", tuple(map(str, self.answer.because)))

    @property
    def answer(self) -> Answer:
        """How it stands as its fact draws it: its word, over why."""

        return Answer(self.word, self.lines, self.tone)

    @property
    def words(self) -> str:
        """How it stands as one line of prose, for a message or a reason."""

        return self.answer.said

    @property
    def run_url(self) -> str:
        """The page of the run it stands on, the preview or the apply; "" for none."""

        return self.run.url if self.run else ""

    @property
    def standing(self) -> Standing | None:
        """How its run stands while it is under way (``work_runs.standing_of``); None otherwise."""

        return run_standing(self.run) if self.live and self.run else None

    @property
    def live(self) -> bool:
        """Whether a step of it is under way, so its card asks again until it ends:
        taken and not yet ended, which is the one standing said in attention."""

        return self.taken and self.tone == Tone.ATTENTION

    @property
    def again(self) -> Asked:
        """What pressing it again answers while it stands taken: where it
        stands, never a refusal, since the first press is still its request."""

        if self.live:
            return Asked(self.standing or Standing(RUNNING, self.words), message=ended(f"Already asked. {self.words}"))
        return Asked(Standing(DONE, self.words), message=ended(self.words))

    def asked(self) -> OperationAsked:
        """``again`` as a repeated ask of the operation it stands on answers
        it, queuing nothing; only Renovate's run stands on none."""

        if self.operation is None:
            raise LookupError("Renovate's run is no operation, so its standing answers as ``again``.")
        said = self.again
        return operation_asked(self.operation, queued=False, standing=said.standing, message=said.message)


@dataclass(frozen=True, slots=True)
class UpdateState:
    """Where one container's update stands, and the one thing to do about it,
    as its finding, the queue and its page all say it (``update_state``)."""

    kind: UpdateKind
    # ``2.16.0 to 2.17.1``: from what runs to the pin the state knows, "" where none.
    move: str
    # The sentence beside the title.
    words: str
    # Where it stands as its fact says it, a word or two over why.
    answer: Answer
    action: UpdateAction | None = None
    # Why no press is offered, "" when one is; and what the press does.
    reason: str = ""
    said: str = ""
    # Held because no one adopted playbook applies it: the Playbooks page's step.
    adopt: bool = False
    # How its approval or its apply stands, once one is asked.
    standing: MergeStanding | None = None
    update: AppUpdate | None = None
    managed: Managed | None = None

    @property
    def at_work(self) -> bool:
        """Whether HQ is moving it now, or moved it: nothing is asked of anyone."""

        return self.standing is not None and self.standing.taken

    @property
    def standing_label(self) -> str:
        """The label its standing is said under, on every page: the press it stands on."""

        return {UpdateKind.OPEN: "Approval", UpdateKind.MERGED: "Apply"}.get(self.kind, "Pull request")


UNOPENED = "Renovate has not seen the release yet."
# What Renovate waits for, by the marker of the box it holds the branch under
# (its ``dependency-dashboard.ts``).
UNTIL = {
    "unpend": "until its checks pass",
    "approve": "until it is approved",
    "unschedule": "until its schedule allows",
    "unlimit": "while it is at its pull request limit",
}


def update_state(item: Container) -> UpdateState:
    """Where ``item``'s update stands: decided here alone, from the open pull
    request, its repository's default branch, Renovate's dashboard and the
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
            Answer("Open", (f"Pull request {update.label}",)),
            UpdateAction(APPROVE, approve_target(update)),
            approval_of(update),
            update=update,
        )
    managed = managed_by(item)
    if managed is None:
        if (unmatched := _unmatched(item)) is not None:
            holder, why = unmatched
            words = ended(f"HQ can't match it to the Compose files in {holder}")
            answer = Answer("Not matched", (f"No Compose file in {holder} matches it",))
            return UpdateState(UpdateKind.UNMATCHED, latest, words, answer, reason=why)
        return UpdateState(UpdateKind.BY_HAND, latest, BY_HAND, Answer("By hand", ("No repository HQ reads holds it",)))
    repository = repositories()[managed.repository]
    if (move := _behind(item, repository, managed.app)) is not None:
        head = str((repository.head or {}).get("sha") or "")
        return _decided(
            UpdateKind.MERGED,
            move,
            ended(f"Merged in {managed.repository}, not applied"),
            Answer("Not applied", (f"Merged in {managed.repository}",)),
            UpdateAction(APPLY, apply_target(managed, head)),
            apply_of(managed.repository, head, (item,)),
            managed=managed,
        )
    action = UpdateAction(OPEN, open_target(managed.repository, managed.app))
    held = held_branch(repository, managed.app)
    standing = open_standing(managed.repository, held)
    # While an Open PR is under way it is held back, as an approval is (``_decided``).
    offered = {"action": action} if standing is None or not standing.taken else {"reason": standing.words}
    if held:
        until = UNTIL.get(str(held.get("marker", "")), "")
        words = ended(" ".join(("Renovate is holding its branch", until)).strip())
        said = f"Ticks {held.get('branch', '')} on the Dependency Dashboard, then asks Renovate to run."
        answer = Answer("Held by Renovate", (capfirst(until),))
        return UpdateState(
            UpdateKind.HELD, latest, words, answer, said=said, standing=standing, managed=managed, **offered
        )
    return UpdateState(
        UpdateKind.UNOPENED,
        latest,
        UNOPENED,
        Answer("Not opened", (UNOPENED,)),
        said="Asks Renovate to run.",
        standing=standing,
        managed=managed,
        **offered,
    )


def _decided(
    kind: UpdateKind, move: str, words: str, answer: Answer, action: UpdateAction, approval: Approval, **of: Any
) -> UpdateState:
    """A state whose press runs while ``approval`` holds nothing back."""

    if approval.held:
        return UpdateState(
            kind, move, words, answer, reason=approval.held, adopt=approval.adopt, standing=approval.standing, **of
        )
    return UpdateState(kind, move, words, answer, action, said=approval.said, standing=approval.standing, **of)


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


def held_branch(repository: Any, app: str) -> Mapping[str, Any] | None:
    """The branch Renovate holds on the Dependency Dashboard for ``app``, with no pull request."""

    return next((each for each in repository.held_branches or () if app in (each.get("apps") or ())), None)


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

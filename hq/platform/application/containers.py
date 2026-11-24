"""Every container that is running, and whether what it runs is okay.

One row per running container, joined from readings HQ already holds: the
sweep's containers, Portainer's images, the ``registry.*`` readings of each
image and its upstream, and for HQ's own image the GitHub App's word on how it
was built. Nothing here reads anything, so the answers are only as fresh as
those readings, and a page says when they were taken.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from hq.domains.control_plane.observations.portainer import (
    IMAGE_KIND as PULLED_KIND,
    RUNTIME_KIND,
    STACK_KIND,
    VOLUME_KIND,
)
from hq.domains.control_plane.observations.public_registry import (
    DIGEST_KIND,
    IMAGE_KIND,
    UPSTREAM_KIND,
    VULNERABILITY_KIND,
)
from hq.domains.control_plane.provider_adapters.portainer import CONTAINER_KIND

from .container_state import ContainerCount, ContainerState, HealthCheck, uptime_of
from .container_words import UP_TO_DATE, UPDATE_AVAILABLE, ImageStandingWords
from .display_words import as_members, display
from .github_public import github_repository
from .images import ImageRef, affected, compare, newer, version
from .moments import moment
from .projection import projection_scope, read_once
from .tone import Tone
from .ui import counted
lazy from . import connections
lazy from .app_updates import builds
lazy from .attestations import github_source
lazy from .container_hardening import hardening_of
lazy from .container_standard import posture_of, runtime_path
lazy from .container_usage import usage_of
lazy from .entity_links import container_link, entity_link
lazy from .facts import inventory_records
lazy from .github_estate import build_of, repositories
lazy from .infrastructure import enabled_resources
lazy from .inventory import record_token
lazy from .machines import machine as machine_named, served_by
lazy from .moments import when
lazy from .projects import projects_by_repository, repository_key
lazy from .supply_chain import supply_chain_of

VULNERABLE = "vulnerable"
BEHIND = "behind"
CURRENT = "current"
UNKNOWN = "unknown"
# Worst first; a level not named sorts after these.
SEVERITIES = ("critical", "high", "medium", "moderate", "low")
SERIOUS = ("critical", "high")
# How HQ knows what an image is built from, most trusted first: the operator's
# word, the image's label, the provenance its publisher attached, the
# registry it lives in.
DECLARED = "declared"
LABEL = "label"
PROVENANCE = "provenance"
REGISTRY = "registry"
# What an image built on the machine that runs it reads as, wherever its standing is said.
BUILT_HERE = "Built on its host"
_KNOWN_BY = {
    DECLARED: "as set in HQ",
    LABEL: "by its label",
    PROVENANCE: "by its provenance",
    REGISTRY: "by its registry",
}


def up_for(status: str, started_at: object, *, running: bool) -> str:
    """How long a container has been up, as HQ writes an age, when Docker's
    inspect gave its start time; Docker's own phrase otherwise.

    The one way any page says it. Docker's phrase is fixed at the moment it
    was read and rounds its own way, so beside an age HQ counts from the start
    time it would say the same fact twice, differently.
    """

    started = moment(str(started_at or ""))
    if started is None or not running:
        return uptime_of(status)
    age = when(started)
    return "Just started" if age == "just now" else f"Up {age.removesuffix(' ago')}"


def started_at(machine: str, container: str) -> str:
    """When Docker's inspect says a container on ``machine`` started, or ""."""

    return str((_runtimes().get((machine, container)) or {}).get("started_at", "") or "")


@dataclass(frozen=True, slots=True)
class Running:
    """A container a controller last saw, described as a person would read it.

    Built from the sweep rather than from a declaration, so every field here is
    something that was true at ``observed_at`` and may not be now. The page says
    when, because a container list with no timestamp invites being read as live.
    """

    name: str
    host: str
    stack: str
    image: str
    state: ContainerState
    status: str
    ports: tuple[int, ...]
    network_mode: str
    host_address: str
    portainer_managed: bool
    connection_ref: str
    observed_at: Any
    # The declaration already watching this, when one is. A field rather than a
    # lookup, because a page renders a table of these and a property would be a
    # query per row, and every row asks the same question of the same table.
    watcher: str = ""
    # Folded away on the machine's page. Still watched, still controllable,
    # this is about where it sits, not about whether HQ can act on it.
    hidden: bool = False
    # Docker's short ID, which is also the hostname inside it.
    id: str = ""
    # The systemd unit its label names, which runs it: its stop, start and
    # restart go through that unit, never Docker.
    unit: str = ""

    @classmethod
    def of(
        cls,
        record: dict[str, Any],
        observed_at: Any,
        watchers: dict[tuple[str, str], tuple[str, bool]] | None = None,
    ) -> Running:
        host = str(record.get("host", ""))
        name = str(record.get("name", ""))
        return cls(
            name=name,
            host=host,
            stack=str(record.get("stack", "")),
            image=str(record.get("image", "")),
            state=ContainerState.read(str(record.get("state", ""))),
            status=str(record.get("status", "")),
            ports=tuple(int(port) for port in record.get("ports") or () if str(port).isdigit()),
            network_mode=str(record.get("network_mode", "")),
            host_address=str(record.get("host_address", "")),
            portainer_managed=bool(record.get("portainer_managed")),
            connection_ref=str(record.get("connection_ref", "")),
            observed_at=observed_at,
            watcher=(watchers or {}).get((host, name), ("", False))[0],
            hidden=(watchers or {}).get((host, name), ("", False))[1],
            id=str(record.get("id", "")),
            unit=str(record.get("unit", "")),
        )

    def __post_init__(self) -> None:
        as_members(self)

    @property
    def uptime(self) -> str:
        return uptime_of(self.status)

    @property
    def check(self) -> HealthCheck | None:
        return HealthCheck.of(self.status)

    @property
    def published(self) -> str:
        """The ports this publishes, or why that cannot be answered.

        A host-network container binds the machine's ports directly and Docker
        reports none for it, so an empty list means "not knowable from here"
        rather than "publishes nothing".
        """

        if self.ports:
            return ", ".join(str(port) for port in self.ports)
        if self.network_mode == "host":
            return "on the host network"
        return ""

    @property
    def token(self) -> str:
        """The handle adoption looks this record up by."""

        return record_token(CONTAINER_KIND, (self.host, self.name))

    @property
    def verbs(self) -> tuple[str, ...]:
        """What it makes sense to ask of a container in this state.

        Offering all three always means offering Start to something already
        running, whose only outcome is Docker answering "already started" a
        minute later in a job result. The state is right here; the buttons
        should read it.
        """

        return ("stop", "restart") if self.state.running else ("start",)

    @property
    def image_label(self) -> str:
        """The image, short enough to read in a card.

        A digest-pinned image is a seventy-character line whose last twelve
        characters are the only part that distinguishes two of them, and printed
        whole it pushed every other fact on the card out of view. The repository
        and the head of the digest is what an operator compares.
        """

        repository, marker, digest = self.image.partition("@")
        if not marker:
            return self.image
        _, _, hexadecimal = digest.partition(":")
        return f"{repository}@{hexadecimal[:12]}"


def socket_holders() -> frozenset[tuple[str, str]]:
    """``(machine, container)`` for each container declared to hold the Docker socket."""

    def load() -> frozenset[tuple[str, str]]:
        return frozenset(
            (str(resource.spec.get("host", "")), str(resource.spec.get("name", "")))
            for resource in enabled_resources()
            if resource.kind == CONTAINER_KIND and resource.spec.get("holds_docker_socket")
        )

    return read_once("containers.socket_holders", load)


def by_design() -> dict[tuple[str, str], dict[str, str]]:
    """``(machine, container)`` to the checks its declaration says its job
    requires it to fail, each with why."""

    def load() -> dict[tuple[str, str], dict[str, str]]:
        return {
            (str(resource.spec.get("host", "")), str(resource.spec.get("name", ""))): dict(resource.spec["by_design"])
            for resource in enabled_resources()
            if resource.kind == CONTAINER_KIND and resource.spec.get("by_design")
        }

    return read_once("containers.by_design", load)


def container_watchers() -> dict[tuple[str, str], tuple[str, bool]]:
    """Which declaration watches which container, and whether it is folded away.

    Keyed on the identity the provider uses, so a container declared in HQ and
    the same container found by a sweep are recognized as one thing. Both facts
    come back together because a page rendering a table of containers asks both
    of every row, and asking twice is two queries for one join.
    """

    return {
        (resource.spec.get("host", ""), resource.spec.get("name", "")): (
            resource.key,
            bool(resource.spec.get("hidden")),
        )
        for resource in enabled_resources()
        if resource.kind == CONTAINER_KIND
    }


@dataclass(frozen=True, slots=True)
class ImageStanding(ImageStandingWords):
    """What is known about one image at one version, wherever it runs."""

    image: ImageRef
    tag: str
    pinned: bool
    # The registry's digest for what runs (``sha256:…``), which is what a scan,
    # a signature and an upgrade are about; and the machine's local image id.
    digest: str = ""
    image_id: str = ""
    # When the image running was built, as the machine's copy says.
    built: datetime | None = None
    # Published tags of the same shape and a higher version, newest first.
    newer: tuple[str, ...] = ()
    # The digest the newest of those names now: what an upgrade would pin.
    target_digest: str = ""
    # The digest the running tag names now, when it is not what runs: the tag
    # was rebuilt or moved since this was pulled.
    moved_to: str = ""
    # The upstream release matching the newest of those, when one does.
    release: Mapping[str, Any] | None = None
    # Advisories that apply to this version, worst first, and to the newest
    # of those newer ones.
    advisories: tuple[Mapping[str, Any], ...] = ()
    target_advisories: tuple[Mapping[str, Any], ...] = ()
    # Advisories whose ranges could not be read against this version.
    unmatched: int = 0
    # The GitHub repository it is built from, as a URL, and how that is known.
    source: str = ""
    source_from: str = ""
    # What its publisher attached to the digest that runs (``registry.digest``)
    # and its packages checked against OSV (``registry.vulnerabilities``), and
    # the same for the digest an upgrade would pin. None where not read.
    attested: Mapping[str, Any] | None = None
    checked: Mapping[str, Any] | None = None
    target_attested: Mapping[str, Any] | None = None
    target_checked: Mapping[str, Any] | None = None
    # Why the registry could not be read, when it could not.
    unread: str = ""
    read_at: datetime | None = None
    upstream_read_at: datetime | None = None
    # HQ's own image: the repository, and whether this digest is signed.
    build: Mapping[str, Any] | None = None
    # A repository HQ's GitHub App reads that builds this image: its own
    # word on the last production deploy stands in for the registry's.
    repository: Any = None
    # Built on the machine that runs it, from Compose files a repository HQ
    # reads: no registry holds it, so none is asked, and its update is that
    # repository's (``app_updates``).
    local: bool = False

    @property
    def upstream(self) -> str:
        """``owner/repository`` of the source, or ""."""

        return self.source.removeprefix("https://github.com/")

    @property
    def source_known_by(self) -> str:
        return _KNOWN_BY.get(self.source_from, "")

    @property
    def provenance(self) -> Mapping[str, Any] | None:
        return (self.attested or {}).get("provenance") or None

    @property
    def built_at(self) -> datetime | None:
        return moment(str((self.provenance or {}).get("finished_at", "") or ""))

    @property
    def checked_at(self) -> datetime | None:
        return moment(str((self.checked or {}).get("read_at", "") or ""))

    @property
    def packages(self) -> int:
        return len((self.attested or {}).get("packages") or ())

    @property
    def commit_url(self) -> str:
        """The commit its provenance names, on its source, when both are known."""

        revision = str((self.provenance or {}).get("revision", "") or "")
        return f"{self.source}/commit/{revision}" if self.source and re.fullmatch(r"[0-9a-f]{7,40}", revision) else ""

    @property
    def built_on(self) -> tuple[str, ...]:
        """The images its provenance says it was built on, as ``name:tag``."""

        found = []
        for uri in (self.provenance or {}).get("materials") or ():
            reference = str(uri).removeprefix("pkg:docker/").partition("?")[0]
            name, _, tag = reference.rpartition("@")
            found.append(f"{name}:{tag}" if name else reference)
        return tuple(dict.fromkeys(found))

    @property
    def findings(self) -> tuple[Mapping[str, Any], ...]:
        """What OSV knows against its packages, worst first."""

        return ranked((self.checked or {}).get("findings") or ())

    @property
    def urgent(self) -> tuple[Mapping[str, Any], ...]:
        """Critical or high, with a fixed version published: what to act on.
        An unrated or unfixed finding is shown, never raised."""

        return tuple(item for item in self.findings if item.get("fixed") and item.get("severity") in SERIOUS)

    @property
    def known_vulnerabilities(self) -> tuple[Mapping[str, Any], ...]:
        """What makes it vulnerable, each once by its id: its advisories, then
        its urgent package findings. A title counts these, and an upgrade
        says which of these it fixes, so both count one thing. Worst first."""

        found: dict[str, Mapping[str, Any]] = {}
        for item in (*self.advisories, *self.urgent):
            found.setdefault(str(item.get("id", "")), item)
        return ranked(found.values())

    @property
    def state(self) -> str:
        if self.known_vulnerabilities:
            return VULNERABLE
        if self.newer:
            return BEHIND
        if self.local or (self.build is not None and self.build.get("signed")):
            return CURRENT
        if self.repository is not None:
            return CURRENT if self.repository.deployed_cleanly else UNKNOWN
        if self.unread or not version(self.tag) or self.read_at is None:
            return UNKNOWN
        return CURRENT

    @property
    def newest(self) -> bool:
        """Its registry was read and publishes nothing newer of this kind,
        whatever else is known about it."""

        return not self.newer and not self.unread and bool(version(self.tag)) and self.read_at is not None

    @property
    def worst(self) -> str:
        found = self.known_vulnerabilities
        return str(found[0].get("severity", "")) if found else ""

    @property
    def serious(self) -> bool:
        return self.worst in SERIOUS

    @property
    def vulnerability_tone(self) -> str:
        """What is known against it as a tone: none where nothing could be checked."""

        if self.serious:
            return Tone.SERIOUS
        if self.state == VULNERABLE:
            return Tone.ATTENTION
        return Tone.GOOD if self.checkable else ""

    @property
    def update_tone(self) -> str:
        """Whether a newer version is out as a tone: none while that is unknown."""

        if self.newer:
            return Tone.ATTENTION
        return Tone.GOOD if self.update_state == UP_TO_DATE else ""

    @property
    def alerts_tone(self) -> str:
        """Its repository's open alerts as a tone, none being good: "" without one."""

        if self.repository is None:
            return ""
        if self.repository.serious_alerts:
            return Tone.SERIOUS
        return Tone.ATTENTION if self.repository.open_alerts else Tone.GOOD

    @property
    def latest(self) -> str:
        return self.newer[0] if self.newer else ""

    @property
    def label(self) -> str:
        return f"{self.image.short}:{self.tag}" if self.tag else self.image.short

    @property
    def summary(self) -> str:
        """The standing in a few words, as every page and the API say it."""

        state = self.state
        if state == VULNERABLE and self.advisories:
            return counted(len(self.known_vulnerabilities), "known vulnerability", "known vulnerabilities")
        if state == VULNERABLE:
            return counted(len(self.known_vulnerabilities), "serious vulnerability", "serious vulnerabilities")
        if state == BEHIND:
            return f"{self.latest} available"
        if state == CURRENT and self.repository is not None:
            return "Verified before deploying" if self.repository.production_verified else "Deployed"
        if state == CURRENT and self.local:
            return BUILT_HERE
        if state == CURRENT:
            return "Up to date"
        return "Update status unknown"


@dataclass(frozen=True, slots=True)
class Container:
    running: Any
    machine: Any
    standing: ImageStanding
    serves: tuple[str, ...] = field(default_factory=tuple)
    # How it is run, from Docker's inspect; None where that was not read.
    runtime: Mapping[str, Any] | None = None

    @property
    def uptime(self) -> str:
        return up_for(self.running.status, (self.runtime or {}).get("started_at"), running=self.running.state.running)

    @property
    def address(self) -> str:
        """``machine:container``: how an API caller names this one. A colon,
        because neither a machine's name nor a container's can hold one."""

        return f"{self.machine.name}:{self.running.name}"

    @property
    def link(self):
        """Its page: its own where a record watches it, else its row on its machine's."""

        return container_link(self.machine.name, self.running.name, self.running.watcher)

    @property
    def usage(self):
        return usage_of(self.runtime)

    @property
    def posture(self):
        return posture_of(self)

    @property
    def hardening(self):
        return hardening_of(self)

    @property
    def supply_chain(self):
        return supply_chain_of(self)

    @property
    def mounts(self) -> tuple[Mapping[str, Any], ...] | None:
        """What it mounts: from Docker's inspect when read, otherwise from the
        data-mount reading, which records the same mounts from the other side.
        None when neither was read, so "no mounts" is never a guess."""

        if self.runtime is not None:
            return tuple(self.runtime.get("mounts") or ())
        return _mounts_from_volumes().get((self.machine.name, self.running.name))

    @property
    def shared_mounts(self) -> dict[str, tuple[Container, ...]]:
        """The other containers on its machine that mount each of its sources."""

        mounts = self.mounts or ()
        wanted = {str(mount.get("source", "")) for mount in mounts}
        users = {
            source: names for source, names in _mount_users().get(self.machine.name, {}).items() if source in wanted
        }
        if not any(name != self.running.name for names in users.values() for name in names):
            return {}
        others = {item.running.name: item for item in containers() if item.machine.name == self.machine.name}
        return {
            source: tuple(others[name] for name in sorted(names) if name != self.running.name and name in others)
            for source, names in users.items()
        }

    @property
    def compose_files(self) -> tuple[str, ...]:
        """The compose files that define it, as its project's reading names them."""

        return tuple(_compose_files().get((self.machine.name, self.running.stack), ()))

    @property
    def project(self) -> Any:
        """The project that tracks the repository its image is built from."""

        return projects_by_repository().get(repository_key(self.standing.source))

    @property
    def url(self) -> str:
        """Its own page when a declaration watches it, else its machine's."""

        if self.running.watcher:
            return entity_link("resource", self.running.watcher).url
        return self.machine.url


def containers() -> list[Container]:
    """Every container the sweep found on every machine, worst standing first.

    Its own projection scope, so every container's standing shares one read of
    each reading however many run; a caller already in one shares that.
    """

    with projection_scope():
        return read_once("containers.containers", _containers)


def _containers() -> list[Container]:
    serves = served_by()
    found = []
    for machine in connections.machines_once():
        found.extend(
            Container(
                running=running,
                machine=machine,
                standing=_standing(running.image, machine.name, running.name),
                serves=tuple(sorted(serves.get((machine.name, running.name), ()))),
                runtime=_runtimes().get((machine.name, running.name)),
            )
            for running in machine.containers
        )
    order = (VULNERABLE, BEHIND, UNKNOWN, CURRENT)
    return sorted(
        found,
        key=lambda item: (
            order.index(item.standing.state),
            not item.standing.serious,
            item.machine.name,
            item.running.name,
        ),
    )


@display
class Tally:
    """What a set of containers comes to, as a band counts it."""

    count: ContainerCount
    # Containers with a known vulnerability, and the images they run.
    vulnerable: int
    vulnerable_images: int
    # Counted per container and apart from vulnerabilities, so with the
    # unknown they add up to the containers a registry holds (``pulled``):
    # a build on its host has no registry's version and no pin.
    update_available: int
    up_to_date: int
    pinned: int
    pulled: int
    # Checked against its source's advisories or its packages against OSV.
    checkable: int

    @property
    def update_unknown(self) -> int:
        return self.pulled - self.update_available - self.up_to_date

    @property
    def unchecked(self) -> int:
        return self.count.total - self.checkable

    @property
    def vulnerable_tone(self) -> str:
        return Tone.SERIOUS if self.vulnerable else Tone.GOOD

    @property
    def updates_tone(self) -> str:
        """Updates as a tone: none until every one is known up to date."""

        if self.update_available:
            return Tone.ATTENTION
        return Tone.GOOD if self.up_to_date == self.pulled else ""

    @property
    def pinned_tone(self) -> str:
        return Tone.GOOD if self.pinned == self.pulled else ""

    @property
    def tone(self) -> str:
        """All of it in one tone, as a machine's band says it."""

        if self.vulnerable or self.count.stopped:
            return Tone.SERIOUS
        return Tone.ATTENTION if self.update_available else Tone.GOOD


def tally(found: list[Container]) -> Tally:
    """What ``found`` comes to."""

    vulnerable = [item.standing for item in found if item.standing.state == VULNERABLE]
    updates = [item.standing.update_state for item in found]
    return Tally(
        count=ContainerCount.of(item.running.state for item in found),
        vulnerable=len(vulnerable),
        vulnerable_images=len({standing.label for standing in vulnerable}),
        update_available=updates.count(UPDATE_AVAILABLE),
        up_to_date=updates.count(UP_TO_DATE),
        pinned=sum(1 for item in found if item.standing.pinned),
        pulled=sum(1 for item in found if not item.standing.local),
        checkable=sum(1 for item in found if item.standing.checkable),
    )


def standings_on(machine: str) -> dict[str, ImageStanding]:
    """Each container on one machine's standing, by container name."""

    return {item.running.name: item.standing for item in containers() if item.machine.name == machine}


def on_machine(machine: Any) -> dict[str, Any]:
    """One machine's containers as its page counts them: each one's standing
    and how many are behind, vulnerable or stopped."""

    here = [item for item in containers() if item.machine.name == machine.name]
    standings = {item.running.name: item.standing for item in here}
    return {
        "shown_containers": machine.on_show,
        "folded_containers": machine.folded,
        "standings": standings,
        "uptimes": {item.running.name: item.uptime for item in here},
        "container_counts": tally(here),
    }


def standing_of(reference: str, host: str = "", container: str = "") -> ImageStanding:
    """What is known about the image ``reference`` names."""

    with projection_scope():
        return _standing(reference, host, container)


def _standing(reference: str, host: str, container: str) -> ImageStanding:
    image = ImageRef.parse(reference)
    if image is None:
        # A container left running an image whose name was removed reports
        # only its id. Still listed, and saying why nothing can be looked up.
        return ImageStanding(
            image=ImageRef("", str(reference or "")[:19]),
            tag="",
            pinned=False,
            unread="It runs an image by id only, so there is no name to look it up by.",
        )
    pulled = _pulled().get((host, container), {})
    if _built_here(host, container, pulled):
        return ImageStanding(
            image=ImageRef("", image.short, image.tag),
            tag=image.tag,
            pinned=False,
            image_id=str(pulled.get("id", "") or ""),
            built=moment(str(pulled.get("created_at", "") or "")),
            local=True,
        )
    tag = image.tag or _pulled_as(image, pulled, "tags", "tag")
    published = _published().get(image.name)
    digest = image.digest or _pulled_as(image, pulled, "digests", "digest")
    attested = _digests().get(f"{image.name}@{digest}") if digest else None
    upstream_name, source_from = source_of(image, published, attested, _declared_sources().get((host, container), ""))
    upstream = _upstreams().get(upstream_name) if upstream_name else None
    later = tuple(newer(tag, (published or {}).get("tags") or ())) if published else ()
    target = str(((published or {}).get("digests") or {}).get(later[0], "") or "") if later else ""
    matched, unmatched = _advisories(tag, upstream)

    return ImageStanding(
        image=image,
        tag=tag,
        pinned=bool(image.digest),
        digest=digest,
        image_id=str(pulled.get("id", "") or ""),
        built=moment(str(pulled.get("created_at", "") or "")),
        newer=later,
        target_digest=target,
        moved_to=_moved_to(tag, digest, published),
        release=_release(later[0], upstream) if later else None,
        advisories=matched,
        target_advisories=_advisories(later[0], upstream)[0] if later else (),
        unmatched=unmatched,
        source=f"https://github.com/{upstream_name}" if upstream_name else "",
        source_from=source_from,
        attested=attested,
        checked=_checked().get(f"{image.name}@{digest}") if digest else None,
        target_attested=_digests().get(f"{image.name}@{target}") if target else None,
        target_checked=_checked().get(f"{image.name}@{target}") if target else None,
        unread=str((published or {}).get("unread", "") or ""),
        read_at=moment(str((published or {}).get("read_at", "") or "")),
        upstream_read_at=moment(str((upstream or {}).get("read_at", "") or "")),
        build=build_of(reference),
        repository=repositories().get(image.github) if image.github else None,
    )


def _built_here(host: str, container: str, pulled: Mapping[str, Any]) -> bool:
    """Whether ``container`` runs an image built on its machine: a copy no
    registry digest names, of a service its Compose files build."""

    if not pulled or pulled.get("digests"):
        return False
    user = next((each for each in pulled.get("containers") or () if each.get("container") == container), {})
    stack = _stacks().get((host, container), "")
    return builds(_compose_files().get((host, stack), ()), str(user.get("service", "") or ""))


def _stacks() -> dict[tuple[str, str], str]:
    """The Compose project each container belongs to, by ``(machine, container)``."""

    def records():
        for record in _on_machines(CONTAINER_KIND):
            yield str(record.get("name", "")), record

    return read_once(
        "containers.stacks",
        lambda: {key: str(record.get("stack", "")) for key, record in _by_container(records()).items()},
    )


def published_digest(image: ImageRef) -> str:
    """The digest ``image`` pins, else the one its registry says its tag names, else ""."""

    return image.digest or str((((_published().get(image.name) or {}).get("digests")) or {}).get(image.tag, "") or "")


def _pulled_as(image: ImageRef, pulled: Mapping[str, Any], field: str, part: str) -> str:
    """The tag (from ``tags``) or registry digest (from ``digests``) the
    machine's copy names for ``image``: what a reference without one was
    pulled as."""

    for named in pulled.get(field) or ():
        found = ImageRef.parse(str(named))
        if found is not None and found.name == image.name and getattr(found, part):
            return str(getattr(found, part))
    return ""


def _moved_to(tag: str, running: str, published: Mapping[str, Any] | None) -> str:
    now = str(((published or {}).get("digests") or {}).get(tag, "") or "")
    return now if now and running and now != running else ""


def source_of(
    image: ImageRef,
    published: Mapping[str, Any] | None,
    attested: Mapping[str, Any] | None,
    declared: str = "",
) -> tuple[str, str]:
    """``(owner/repository, how it is known)`` for what an image is built from."""

    labeled = github_repository(str((published or {}).get("source", "") or ""))
    for named, how in (
        (declared, DECLARED),
        ("/".join(labeled) if labeled else "", LABEL),
        (github_source(str(((attested or {}).get("provenance") or {}).get("source", "") or "")), PROVENANCE),
        (image.github, REGISTRY),
    ):
        if named:
            return named, how
    return "", ""


def ranked(findings) -> tuple[Mapping[str, Any], ...]:
    """Worst first; unrated last."""

    rank = {level: index for index, level in enumerate(SEVERITIES)}
    return tuple(sorted(findings, key=lambda item: rank.get(str(item.get("severity", "")), len(SEVERITIES))))


def _advisories(tag: str, upstream: Mapping[str, Any] | None) -> tuple[tuple[Mapping[str, Any], ...], int]:
    matched = []
    unmatched = 0
    for item in (upstream or {}).get("advisories") or ():
        verdict = affected(tag, [tuple(pair) for pair in item.get("vulnerabilities") or ()])
        if verdict is True:
            matched.append(item)
        elif verdict is None:
            unmatched += 1
    return ranked(matched), unmatched


def _release(tag: str, upstream: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    wanted = version(tag)
    for item in (upstream or {}).get("releases") or ():
        if compare(version(str(item.get("tag", ""))), wanted) == 0:
            return {**item, "published": moment(str(item.get("published_at", "") or ""))}
    return None


def _pulled() -> dict[tuple[str, str], Mapping[str, Any]]:
    """The machine's copy of the image each container runs, by ``(machine, container)``."""

    def records():
        for record in _on_machines(PULLED_KIND):
            for user in record.get("containers") or ():
                yield str(user.get("container", "")), record

    return read_once("containers.pulled", lambda: _by_container(records()))


def _runtimes() -> dict[tuple[str, str], Mapping[str, Any]]:
    """How each container is run, by ``(machine, container)``."""

    def records():
        for record in _on_machines(RUNTIME_KIND):
            yield str(record.get("container", "")), record

    return read_once("containers.runtimes", lambda: _by_container(records()))


def _mounts_from_volumes() -> dict[tuple[str, str], tuple[Mapping[str, Any], ...]]:
    """Each container's mounts as the data-mount reading records them, keyed
    ``(machine, container)``: every volume and bind, and who uses it how."""

    def load() -> dict[tuple[str, str], tuple[Mapping[str, Any], ...]]:
        found: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
        for record in _on_machines(VOLUME_KIND):
            source = str(record.get("name") if record.get("type") == "volume" else record.get("source") or "")
            for user in record.get("used_by") or ():
                found.setdefault((str(record.get("host", "")), str(user.get("container", ""))), []).append(
                    {
                        "type": str(record.get("type", "")),
                        "source": source,
                        "destination": str(user.get("destination", "")),
                        "read_only": bool(user.get("read_only")),
                    }
                )
        return {key: tuple(value) for key, value in found.items()}

    return read_once("containers.mounts", load)


def _mount_users() -> dict[str, dict[str, set[str]]]:
    """``{machine: {source: container names}}`` from the data-mount reading."""

    def load() -> dict[str, dict[str, set[str]]]:
        found: dict[str, dict[str, set[str]]] = {}
        for record in _on_machines(VOLUME_KIND):
            source = str(record.get("name") if record.get("type") == "volume" else record.get("source") or "")
            names = {str(user.get("container", "")) for user in record.get("used_by") or ()} - {""}
            found.setdefault(str(record.get("host", "")), {}).setdefault(source, set()).update(names)
        return found

    return read_once("containers.mount_users", load)


def _compose_files() -> dict[tuple[str, str], tuple[str, ...]]:
    def load() -> dict[tuple[str, str], tuple[str, ...]]:
        return {
            # A compose file under a runtime directory was a copy made to start
            # it (a deploy works from one), gone once it has: not where it is
            # defined, and not a path anyone can open.
            (str(record.get("host", "")), str(record.get("name", ""))): tuple(
                path for path in record.get("config_files") or () if not runtime_path(str(path))
            )
            for record in _on_machines(STACK_KIND)
        }

    return read_once("containers.compose_files", load)


def _on_machines(kind: str):
    """A Portainer reading's records, each with the machine it is on named as HQ names it."""

    for _snapshot, record in inventory_records(kind):
        host = str(record.get("host", ""))
        named = machine_named(host)
        yield {**record, "host": named.name if named else host}


def _by_container(pairs) -> dict[tuple[str, str], Mapping[str, Any]]:
    return {(str(record.get("host", "")), container): record for container, record in pairs if container}


def _published() -> dict[str, Mapping[str, Any]]:
    return read_once("containers.published", lambda: _by(IMAGE_KIND, "image"))


def _upstreams() -> dict[str, Mapping[str, Any]]:
    return read_once("containers.upstreams", lambda: _by(UPSTREAM_KIND, "repository"))


def _digests() -> dict[str, Mapping[str, Any]]:
    return read_once("containers.digests", lambda: _by(DIGEST_KIND, "digest"))


def _checked() -> dict[str, Mapping[str, Any]]:
    return read_once("containers.checked", lambda: _by(VULNERABILITY_KIND, "digest"))


def _declared_sources() -> dict[tuple[str, str], str]:
    """``owner/repository`` a container's declaration names as its source, by
    ``(machine, container)``: the operator's word, for an image that names none."""

    def load() -> dict[tuple[str, str], str]:
        found = {}
        for resource in enabled_resources():
            named = (
                github_repository(str(resource.spec.get("source", "") or ""))
                if resource.kind == CONTAINER_KIND
                else None
            )
            if named:
                host = str(resource.spec.get("host", ""))
                on = machine_named(host)
                found[(on.name if on else host, str(resource.spec.get("name", "")))] = "/".join(named)
        return found

    return read_once("containers.declared_sources", load)


def _by(kind: str, key: str) -> dict[str, Mapping[str, Any]]:
    return {str(record.get(key, "")): record for _snapshot, record in inventory_records(kind) if record.get(key)}

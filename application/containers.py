"""Every container that is running, and whether what it runs is okay.

One row per running container, joined from what HQ already reads: the sweep's
container list (where it runs, its image reference), Portainer's image list
(what that reference was when pulled), the public registry's tags for the image
(``registry.image``), what its publisher attached to the digest that runs: its
package list and provenance (``registry.digest``), those packages checked
against OSV (``registry.vulnerabilities``), and the releases and advisories on
the GitHub repository it is built from (``registry.upstream``). HQ's own image
adds the GitHub App's word on how it was built (``github_estate.build_of``).

Nothing here reads anything. The answers are only as fresh as those readings,
and a page says when they were taken.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping


from control_plane.observations.portainer import (
    IMAGE_KIND as PULLED_KIND,
    RUNTIME_KIND,
    STACK_KIND,
    VOLUME_KIND,
)
from control_plane.observations.public_registry import (
    DIGEST_KIND,
    IMAGE_KIND,
    UPSTREAM_KIND,
    VULNERABILITY_KIND,
)
from control_plane.provider_adapters.portainer import CONTAINER_KIND

from .github_public import github_repository
from .images import ImageRef, affected, compare, newer, version
from .projection import projection_scope, read_once
from .ui import Insight, counted, moment

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
_KNOWN_BY = {DECLARED: "as declared", LABEL: "by its label", PROVENANCE: "by its provenance", REGISTRY: "by its registry"}


# Docker's status ends with its health check's verdict: "Up 11 hours (healthy)".
_CHECK = re.compile(r"\s*\((healthy|unhealthy|health: [^)]*)\)\s*$")


def uptime_of(status: str) -> str:
    """Docker's status without its health check: ``Up 11 hours``."""

    return _CHECK.sub("", str(status or "")).strip()


def check_of(status: str) -> str:
    """What the container's own health check says, in words, or "" without one."""

    found = _CHECK.search(str(status or ""))
    if not found:
        return ""
    verdict = found.group(1)
    if verdict == "healthy":
        return "health check passing"
    if verdict == "unhealthy":
        return "health check failing"
    return f"health check {verdict.removeprefix('health: ')}"


@dataclass(frozen=True)
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
    state: str
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

    @classmethod
    def of(
        cls,
        record: dict[str, Any],
        observed_at: Any,
        watchers: dict[tuple[str, str], tuple[str, bool]] | None = None,
    ) -> "Running":
        host = str(record.get("host", ""))
        name = str(record.get("name", ""))
        return cls(
            name=name,
            host=host,
            stack=str(record.get("stack", "")),
            image=str(record.get("image", "")),
            state=str(record.get("state", "")),
            status=str(record.get("status", "")),
            ports=tuple(
                int(port) for port in record.get("ports") or () if str(port).isdigit()
            ),
            network_mode=str(record.get("network_mode", "")),
            host_address=str(record.get("host_address", "")),
            portainer_managed=bool(record.get("portainer_managed")),
            connection_ref=str(record.get("connection_ref", "")),
            observed_at=observed_at,
            watcher=(watchers or {}).get((host, name), ("", False))[0],
            hidden=(watchers or {}).get((host, name), ("", False))[1],
            id=str(record.get("id", "")),
        )

    @property
    def healthy(self) -> bool:
        return self.state == "running"

    @property
    def uptime(self) -> str:
        return uptime_of(self.status)

    @property
    def check(self) -> str:
        return check_of(self.status)

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

        from .inventory import record_token

        return record_token(CONTAINER_KIND, (self.host, self.name))

    @property
    def verbs(self) -> tuple[str, ...]:
        """What it makes sense to ask of a container in this state.

        Offering all three always means offering Start to something already
        running, whose only outcome is Docker answering "already started" a
        minute later in a job result. The state is right here; the buttons
        should read it.
        """

        return ("stop", "restart") if self.healthy else ("start",)

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

    from .infrastructure import enabled_resources

    def load() -> frozenset[tuple[str, str]]:
        return frozenset(
            (str(resource.spec.get("host", "")), str(resource.spec.get("name", "")))
            for resource in enabled_resources()
            if resource.kind == CONTAINER_KIND and resource.spec.get("holds_docker_socket")
        )

    return read_once("containers.socket_holders", load)


def container_watchers() -> dict[tuple[str, str], tuple[str, bool]]:
    """Which declaration watches which container, and whether it is folded away.

    Keyed on the identity the provider uses, so a container declared in HQ and
    the same container found by a sweep are recognised as one thing. Both facts
    come back together because a page rendering a table of containers asks both
    of every row, and asking twice is two queries for one join.
    """

    from .infrastructure import enabled_resources

    return {
        (resource.spec.get("host", ""), resource.spec.get("name", "")): (
            resource.key,
            bool(resource.spec.get("hidden")),
        )
        for resource in enabled_resources()
        if resource.kind == CONTAINER_KIND
    }


@dataclass(frozen=True)
class Standing:
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
    # Advisories that apply to this version, worst first.
    advisories: tuple[Mapping[str, Any], ...] = ()
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
    def state(self) -> str:
        if self.advisories or self.urgent:
            return VULNERABLE
        if self.newer:
            return BEHIND
        if self.build is not None and self.build.get("signed"):
            return CURRENT
        if self.repository is not None:
            return CURRENT if self.repository.production_verified else UNKNOWN
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
        levels = [str(item.get("severity", "")) for item in (*self.advisories[:1], *self.urgent[:1])]
        return ranked([{"severity": level} for level in levels])[0]["severity"] if levels else ""

    @property
    def serious(self) -> bool:
        return self.worst in SERIOUS

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
            return counted(len(self.advisories), "known advisory", "known advisories")
        if state == VULNERABLE:
            return f"{len(self.urgent)} serious, fixable"
        if state == BEHIND:
            return f"{self.latest} is out"
        if state == CURRENT and self.repository is not None:
            return "Verified before deploying" if self.repository.production_verified else "Deployed"
        if state == CURRENT:
            return "Newest release"
        return "Not known"


@dataclass(frozen=True)
class Container:
    running: Any
    machine: Any
    standing: Standing
    serves: tuple[str, ...] = field(default_factory=tuple)
    # How it is run, from Docker's inspect; None where that was not read.
    runtime: Mapping[str, Any] | None = None

    @property
    def address(self) -> str:
        """``machine:container``: how an API caller names this one. A colon,
        because neither a machine's name nor a container's can hold one."""

        return f"{self.machine.name}:{self.running.name}"

    @property
    def posture(self):
        from .container_standard import posture_of

        return posture_of(self)

    @property
    def hardening(self):
        from .container_hardening import hardening_of

        return hardening_of(self)

    @property
    def supply_chain(self):
        from .supply_chain import supply_chain_of

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
    def shared_mounts(self) -> dict[str, tuple["Container", ...]]:
        """The other containers on its machine that mount each of its sources."""

        mounts = self.mounts or ()
        wanted = {str(mount.get("source", "")) for mount in mounts}
        users = {
            source: names
            for source, names in _mount_users().get(self.machine.name, {}).items()
            if source in wanted
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

        repository = self.standing.source.removeprefix("https://github.com/")
        return _projects_by_repository().get(repository.lower()) if repository else None

    @property
    def url(self) -> str:
        """Its own page when a declaration watches it, else its machine's."""

        from .entity_links import entity_link

        if self.running.watcher:
            return entity_link("resource", self.running.watcher).url
        return self.machine.url


def containers() -> list[Container]:
    """Every running container on every machine, worst standing first.

    Its own projection scope, so every container's standing shares one read of
    each reading however many run; a caller already in one shares that.
    """

    with projection_scope():
        return read_once("containers.containers", _containers)


def _containers() -> list[Container]:
    from .connections import machines_once
    from .machines import served_by

    serves = served_by()
    found = []
    for machine in machines_once():
        for running in machine.containers:
            found.append(
                Container(
                    running=running,
                    machine=machine,
                    standing=_standing(running.image, machine.name, running.name),
                    serves=tuple(sorted(serves.get((machine.name, running.name), ()))),
                    runtime=_runtimes().get((machine.name, running.name)),
                )
            )
    order = (VULNERABLE, BEHIND, UNKNOWN, CURRENT)
    return sorted(
        found,
        key=lambda item: (order.index(item.standing.state), not item.standing.serious, item.machine.name, item.running.name),
    )


def standings_on(machine: str) -> dict[str, Standing]:
    """Each container on one machine's standing, by container name."""

    return {item.running.name: item.standing for item in containers() if item.machine.name == machine}


def on_machine(machine: Any) -> dict[str, Any]:
    """One machine's containers as its page counts them: each one's standing,
    how many are behind, vulnerable or stopped, and the images it keeps that
    nothing runs, with the space they take."""

    from .docker_sections import unused_images
    from .labels import human_bytes

    standings = standings_on(machine.name)
    states = [standing.state for standing in standings.values()]
    unused = unused_images(machine)
    return {
        "standings": standings,
        "container_counts": {
            "behind": states.count(BEHIND),
            "current": states.count(CURRENT),
            "vulnerable": states.count(VULNERABLE),
            "stopped": sum(1 for item in machine.containers if not item.healthy),
        },
        "unused_images": unused,
        "unused_size": human_bytes(sum(record.get("size") or 0 for record in unused)),
    }


def standing_of(reference: str, host: str = "", container: str = "") -> Standing:
    """What is known about the image ``reference`` names."""

    with projection_scope():
        return _standing(reference, host, container)


def _standing(reference: str, host: str, container: str) -> Standing:
    image = ImageRef.parse(reference)
    if image is None:
        # A container left running an image whose name was removed reports
        # only its id. Still listed, and saying why nothing can be looked up.
        return Standing(
            image=ImageRef("", str(reference or "")[:19]),
            tag="",
            pinned=False,
            unread="It runs an image by id alone, so there is no name to look it up by.",
        )
    pulled = _pulled().get((host, container), {})
    tag = image.tag or _pulled_as(image, pulled, "tags", "tag")
    published = _published().get(image.name)
    digest = image.digest or _pulled_as(image, pulled, "digests", "digest")
    attested = _digests().get(f"{image.name}@{digest}") if digest else None
    upstream_name, source_from = source_of(image, published, attested, _declared_sources().get((host, container), ""))
    upstream = _upstreams().get(upstream_name) if upstream_name else None
    later = tuple(newer(tag, (published or {}).get("tags") or ())) if published else ()
    target = str(((published or {}).get("digests") or {}).get(later[0], "") or "") if later else ""
    matched, unmatched = _advisories(tag, upstream)
    from .github_estate import build_of, repositories

    return Standing(
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

    from .attestations import github_source

    labelled = github_repository(str((published or {}).get("source", "") or ""))
    for named, how in (
        (declared, DECLARED),
        ("/".join(labelled) if labelled else "", LABEL),
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
    from .container_standard import runtime_path

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

    from .facts import inventory_records
    from .machines import machine

    for _snapshot, record in inventory_records(kind):
        host = str(record.get("host", ""))
        named = machine(host)
        yield {**record, "host": named.name if named else host}


def _by_container(pairs) -> dict[tuple[str, str], Mapping[str, Any]]:
    return {(str(record.get("host", "")), container): record for container, record in pairs if container}


def _projects_by_repository() -> dict[str, Any]:
    """Projects by the ``owner/repository`` their repository URL names, lower-cased."""

    def load() -> dict[str, Any]:
        from projects.models import Project

        from .github_public import github_repository

        return {
            "/".join(parts).lower(): project
            for project in Project.objects.exclude(repository_url="")
            if (parts := github_repository(project.repository_url))
        }

    return read_once("containers.projects", load)


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
        from .infrastructure import enabled_resources
        from .machines import machine

        found = {}
        for resource in enabled_resources():
            named = github_repository(str(resource.spec.get("source", "") or "")) if resource.kind == CONTAINER_KIND else None
            if named:
                host = str(resource.spec.get("host", ""))
                on = machine(host)
                found[(on.name if on else host, str(resource.spec.get("name", "")))] = "/".join(named)
        return found

    return read_once("containers.declared_sources", load)


def _by(kind: str, key: str) -> dict[str, Mapping[str, Any]]:
    from .facts import inventory_records

    return {str(record.get(key, "")): record for _snapshot, record in inventory_records(kind) if record.get(key)}


def attention() -> tuple[Insight, ...]:
    """What needs doing about the containers, each item with the help HQ
    derived for it (``application.container_attention``)."""

    from .container_attention import attention as items

    return items()

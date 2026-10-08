"""Readings HQ takes itself from the keyless public registries.

RDAP, an image registry's public tags, labels and attestations, the releases
and advisories on the GitHub repository an image is built from, and OSV's
vulnerability database need no credential, so HQ reads them rather than a
controller. ``refresh`` runs as the scheduled ``registry.refresh`` job, which
a timer asks for once a day and HQ starts itself when a sweep finds an image
or digest it has not read. It reads
only subjects that are due, at most a budget of them, and stores the result
through the same ingest as a sweep. Pages read the stored reading through
``application.facts`` and never wait on a registry.

Each reading stands as long as what it reads is slow to change
(``READ_EVERY``); a digest's attestations never change, so each is read once.
"""

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.utils import timezone

from hq.domains.control_plane.dns_lookup import (
    LookupNotFound,
    LookupUnavailable,
    domain_registry,
    registry,
)
from hq.domains.control_plane.observations.public_registry import (
    ADDRESS_KIND,
    DIGEST_KIND,
    DOMAIN_KIND,
    IMAGE_KIND,
    UPSTREAM_KIND,
    VULNERABILITY_KIND,
)

from .facts import record_read_at
from .locate import host_of
from .lookup import HOSTNAME, allocation_of, entity_name
from .reach import is_public, public_label
from .security import Capability, Principal

REFRESH_AFTER = timedelta(days=1)
# How long each reading stands, by how fast what it reads changes: who holds
# an address or registers a domain moves in years; a tag, a release or a
# vulnerability can land any day. A digest never changes (``None``).
READ_EVERY: dict[str, timedelta | None] = {
    ADDRESS_KIND: timedelta(days=7),
    DOMAIN_KIND: timedelta(days=7),
    IMAGE_KIND: REFRESH_AFTER,
    UPSTREAM_KIND: REFRESH_AFTER,
    DIGEST_KIND: None,
    VULNERABILITY_KIND: REFRESH_AFTER,
}
# A subject that could not be read is asked again after this, not on every run.
RETRY_AFTER = timedelta(hours=1)
LOOKUPS_PER_REFRESH = 16
# GitHub allows 60 anonymous reads an hour, shared with Watching; each
# repository costs two.
UPSTREAMS_PER_REFRESH = 8
UPSTREAM_RELEASES = 10
# Each vulnerability's detail is read once and kept until OSV modifies it.
DETAILS_PER_REFRESH = 400


def read_every(kind: str) -> timedelta:
    """How long ``kind``'s reading stands; a digest's, as long as a day's check
    that every running digest is held."""

    return READ_EVERY.get(kind) or REFRESH_AFTER


def _configured() -> bool:
    endpoint = str(getattr(settings, "SEVERINO_RDAP_ENDPOINT", "") or "").strip()
    parsed = urlsplit(endpoint)
    return parsed.scheme == "https" and bool(parsed.hostname)


def public_address(value: str) -> str:
    """The address, normalised, when it is routable on the public internet."""

    try:
        parsed = ip_address(host_of(value))
    except ValueError:
        return ""
    return str(parsed) if is_public(str(parsed)) else ""


def wanted_addresses() -> tuple[str, ...]:
    """Public addresses a service is served from, a machine is declared at, or a
    machine's tailnet client reports."""

    from .infrastructure import declared_machines
    from .services import service_catalog
    from .tailnet_presence import tailnet_presence

    found = {
        public_address(service.origin.address)
        for service in service_catalog()
        if service.origin is not None
    }
    for machine in declared_machines():
        found.update(public_address(str(item)) for item in machine.get("addresses") or ())
    # Already filtered to public addresses by the reach ranges.
    for presence in tailnet_presence().values():
        found.update(presence.public_addresses)
    return tuple(sorted(address for address in found if address))


def holders(addresses) -> dict[str, str]:
    """Who holds each public address, from the stored registry reading."""

    from .facts import Subject, readings

    index = readings()
    found = {}
    for address in addresses:
        holder = next(
            (
                item.title
                for item in index.about(Subject.of(addresses=(address,)), kinds=(ADDRESS_KIND,))
                if item.title
            ),
            "",
        )
        found[address] = holder
    return found


def public_endpoints(addresses) -> tuple[tuple[str, str], ...]:
    """``(address, holder)`` with each IPv6 address folded into its /64, once.

    A client rotates IPv6 privacy addresses inside one /64; the prefix is the
    stable fact. The holder is the first one read for any address it folds.
    """

    held = holders(addresses)
    shown: dict[str, str] = {}
    for address in addresses:
        label = public_label(address)
        if label and not shown.get(label):
            shown[label] = held.get(address, "")
    return tuple(shown.items())


def wanted_domains() -> tuple[str, ...]:
    from .zones import zone_names

    return tuple(name for name in zone_names() if HOSTNAME.match(name))


def read_address(address: str, allocations: Callable[..., dict] = registry) -> dict[str, Any]:
    held = allocations(address)
    allocation = allocation_of(held)
    return {
        "address": address,
        "organisation": allocation["organisation"],
        "network": allocation["name"],
        "handle": allocation["handle"],
        "country": allocation["country"],
        "range": allocation["range"],
    }


def read_domain(domain: str, registrations: Callable[..., dict] = domain_registry) -> dict[str, Any]:
    held = registrations(domain)
    events = {
        str(event.get("eventAction", "")): str(event.get("eventDate", ""))
        for event in held.get("events") or ()
        if isinstance(event, dict)
    }
    return {
        "domain": domain,
        "registrar": entity_name(held, "registrar"),
        "registered_at": events.get("registration", ""),
        "expires_at": events.get("expiration", ""),
        "status": [str(item) for item in held.get("status") or () if item],
    }


def wanted_images() -> dict[str, tuple[str, ...]]:
    """``{registry/repository: the references running}`` for every image a container runs."""

    from hq.domains.control_plane.observations.portainer import IMAGE_KIND as PULLED_KIND
    from hq.domains.control_plane.provider_adapters.portainer import CONTAINER_KIND

    from .facts import inventory_records
    from .images import ImageRef

    # A reference pinned by digest alone names no tag; the machine's copy says
    # which tag it was pulled as, so that tag's digest can be read too.
    pulled_as: dict[tuple[str, str], list[str]] = {}
    for _snapshot, pulled in inventory_records(PULLED_KIND):
        for user in pulled.get("containers") or ():
            pulled_as.setdefault((str(pulled.get("host", "")), str(user.get("container", ""))), []).extend(
                str(tag) for tag in pulled.get("tags") or ()
            )
    found: dict[str, set[str]] = {}
    for _snapshot, record in inventory_records(CONTAINER_KIND):
        image = ImageRef.parse(str(record.get("image", "") or ""))
        if image is None:
            continue
        found.setdefault(image.name, set()).add(str(record.get("image", "")))
        if not image.tag:
            for tagged in pulled_as.get((str(record.get("host", "")), str(record.get("name", ""))), ()):
                named = ImageRef.parse(tagged)
                if named is not None and named.name == image.name and named.tag:
                    found[image.name].add(tagged)
    return {name: tuple(sorted(references)) for name, references in sorted(found.items())}


def wanted_upstreams() -> tuple[str, ...]:
    """``owner/repository`` for every GitHub repository a running image is
    built from, however that is known (``application.containers.source_of``)."""

    from .containers import containers

    return tuple(sorted({item.standing.upstream for item in containers() if item.standing.upstream}))


def wanted_digests() -> tuple[str, ...]:
    """``registry/repository@sha256:…`` for what runs, what its tag names now,
    and what an upgrade would pin: every digest a page compares."""

    from .containers import containers

    found = set()
    for item in containers():
        standing = item.standing
        for digest in (standing.digest, standing.moved_to, standing.target_digest):
            if digest and standing.image.name:
                found.add(f"{standing.image.name}@{digest}")
    return tuple(sorted(found))


def wanted_vulnerabilities() -> tuple[str, ...]:
    """The digests whose publisher attached a package list to check."""

    from .facts import inventory_records

    wanted = set(wanted_digests())
    return tuple(
        sorted(
            str(record.get("digest"))
            for _snapshot, record in inventory_records(DIGEST_KIND)
            if record.get("digest") in wanted and record.get("packages")
        )
    )


def read_image(name: str, references: tuple[str, ...]) -> dict[str, Any]:
    """The image's version tags and build labels, and the digest behind each
    running tag and the newest tag of the same shape: what an upgrade would pin."""

    from .images import ImageRef, newer, version
    from .oci_registry import RegistryPrivate, RegistryReadError, digest_of, labels, tags

    images = [image for image in (ImageRef.parse(reference) for reference in references) if image is not None]
    if not images:
        raise LookupNotFound(f"{name} names no image.")
    try:
        listed = [tag for tag in tags(images[0]) if version(tag) and not tag.startswith("sha256-")]
    except RegistryPrivate as exc:
        raise LookupNotFound(str(exc)) from exc
    except RegistryReadError as exc:
        raise LookupUnavailable(str(exc)) from exc
    try:
        built = labels(images[0])
    except RegistryReadError:
        # The tags are the reading; the labels only add where it came from.
        built = {}
    wanted = {image.tag for image in images if image.tag}
    wanted |= {newer(tag, listed)[0] for tag in list(wanted) if newer(tag, listed)}
    digests = {}
    unresolved = []
    for tag in sorted(wanted):
        try:
            digests[tag] = digest_of(images[0], tag)
        except RegistryReadError:
            digests[tag] = ""
        if not digests[tag]:
            unresolved.append(tag)
    return {
        "image": name,
        "tags": listed,
        "digests": digests,
        # Due again on the next refresh rather than in a day: a digest an
        # upgrade would pin is worth the extra read.
        "unresolved": unresolved,
        "source": built.get("org.opencontainers.image.source", ""),
        "revision": built.get("org.opencontainers.image.revision", ""),
        "version": built.get("org.opencontainers.image.version", ""),
    }


def read_digest(key: str) -> dict[str, Any]:
    """The SBOM and provenance attached to one digest, reduced to what HQ uses."""

    from .attestations import reduce
    from .images import ImageRef
    from .oci_registry import RegistryPrivate, RegistryReadError, attestations

    image = ImageRef.parse(key)
    if image is None or not image.digest:
        raise LookupNotFound(f"{key} names no digest.")
    try:
        attached = attestations(image, image.digest)
    except RegistryPrivate as exc:
        raise LookupNotFound(str(exc)) from exc
    except RegistryReadError as exc:
        raise LookupUnavailable(str(exc)) from exc
    return {"digest": key, "image": image.name, "platform_digest": attached["platform_digest"], **reduce(attached["statements"])}


def vulnerability_reader(
    kept: dict[tuple[str, str, str], Mapping[str, Any]], budget: int = DETAILS_PER_REFRESH
) -> Callable[[str], dict[str, Any]]:
    """A reader for ``registry.vulnerabilities`` that reads each detail once.

    ``kept`` is every finding already held, by ``(id, package, installed)``;
    one OSV has not modified since is kept as it is. The budget is shared by
    every digest one refresh reads, and what it leaves is read on the next.
    """

    from .facts import inventory_records
    from .osv import OSVReadError, detail, finding, matches

    packages = {
        str(record.get("digest")): tuple(record.get("packages") or ())
        for _snapshot, record in inventory_records(DIGEST_KIND)
    }
    remaining = [budget]

    def one(identifier: str, modified: str, purl: str) -> tuple[Mapping[str, Any], bool]:
        bare = finding({"id": identifier}, purl, modified)
        held = kept.get((identifier, bare["package"], bare["installed"]))
        if held is not None and held.get("modified") == modified:
            return held, True
        if remaining[0] <= 0:
            return {**bare, "modified": ""}, False
        remaining[0] -= 1
        try:
            return finding(detail(identifier), purl, modified), True
        except OSVReadError:
            return {**bare, "modified": ""}, False

    def read(key: str) -> dict[str, Any]:
        try:
            checked, found = matches(packages.get(key, ()))
        except OSVReadError as exc:
            raise LookupUnavailable(str(exc)) from exc
        findings, unresolved = [], []
        for purl, ids in found.items():
            for identifier, modified in ids:
                shaped, complete = one(identifier, modified, purl)
                findings.append(shaped)
                if not complete:
                    unresolved.append(identifier)
        # Due again on the next refresh while any detail is missing.
        return {"digest": key, "checked": checked, "findings": findings, "unresolved": sorted(set(unresolved))}

    return read


def read_vulnerabilities(key: str) -> dict[str, Any]:
    """One digest's packages checked against OSV, every detail read afresh."""

    return vulnerability_reader({})(key)


def _github_link(value: Any) -> str:
    """A link GitHub gave, kept only when it is a GitHub page: it becomes an href."""

    text = str(value or "")
    return text if text.startswith("https://github.com/") else ""


def read_upstream(repository: str) -> dict[str, Any]:
    from .github_public import GitHubReadError, get

    try:
        releases = get(f"/repos/{repository}/releases?per_page={UPSTREAM_RELEASES}", missing_ok=True)
        advisories = get(f"/repos/{repository}/security-advisories?state=published&per_page=100", missing_ok=True)
    except GitHubReadError as exc:
        raise LookupUnavailable(str(exc)) from exc
    if releases is None and advisories is None:
        raise LookupNotFound(f"GitHub has no public repository {repository}.")
    return {
        "repository": repository,
        "url": f"https://github.com/{repository}",
        "releases": [
            {
                "tag": str(item.get("tag_name") or ""),
                "url": _github_link(item.get("html_url")),
                "published_at": str(item.get("published_at") or ""),
            }
            for item in releases or ()
            if isinstance(item, dict) and item.get("tag_name") and not item.get("prerelease") and not item.get("draft")
        ],
        "advisories": [
            {
                "id": str(item.get("cve_id") or item.get("ghsa_id") or ""),
                "severity": str(item.get("severity") or ""),
                "summary": str(item.get("summary") or ""),
                "url": _github_link(item.get("html_url")),
                "published_at": str(item.get("published_at") or ""),
                "vulnerabilities": [
                    [str(entry.get("vulnerable_version_range") or ""), str(entry.get("patched_versions") or "")]
                    for entry in item.get("vulnerabilities") or ()
                    if isinstance(entry, dict)
                ],
            }
            for item in advisories or ()
            if isinstance(item, dict)
        ],
    }


def _due(record: Mapping[str, Any] | None, every: timedelta | None, now: datetime, force: bool) -> bool:
    """Whether one subject's record is due to be read again."""

    if record is None or record.get("unresolved"):
        return True
    read_at = record_read_at(record) or datetime.min.replace(tzinfo=now.tzinfo)
    if record.get("unread"):
        return read_at < now - RETRY_AFTER
    if every is None:
        # What a digest carries never changes.
        return False
    return force or read_at < now - every


def _report(
    kind: str,
    key: str,
    subjects: Iterable[str],
    read: Callable[[str], dict[str, Any]],
    now: datetime,
    budget: int = LOOKUPS_PER_REFRESH,
    force: bool = False,
) -> dict[str, Any] | None:
    """One kind's payload for the ingest, or None when nothing is due.

    A reading of what never changes is still stored again once a day when
    nothing was read, so its age says when HQ last checked it held all of it.
    """

    from hq.domains.control_plane.models import ProviderInventory

    stored = ProviderInventory.objects.filter(kind=kind).first()
    kept = {
        str(record.get(key, "")): record
        for record in (stored.records if stored is not None else ())
    }
    wanted = tuple(dict.fromkeys(subjects))
    every = READ_EVERY.get(kind, REFRESH_AFTER)
    due = [subject for subject in wanted if _due(kept.get(subject), every, now, force)]
    dropped = set(kept) - set(wanted)
    current = stored is not None and (every is not None or stored.observed_at >= now - read_every(kind))
    if not due and not dropped and stored is not None and stored.reachable and current:
        return None
    answered = 0
    failures: list[str] = []
    for subject in due[:budget]:
        try:
            record = read(subject)
        except LookupNotFound as exc:
            record = {key: subject, "unread": str(exc)}
            answered += 1
        except LookupUnavailable as exc:
            failures.append(str(exc))
            record = {key: subject, "unread": str(exc)}
        else:
            answered += 1
        kept[subject] = {**record, "read_at": now.isoformat()}
    # Unreadable only when nothing can be said at all. One subject failing
    # while others were read before must not discard those readings: an empty
    # report would replace every image's stored reading.
    readable = any(not kept[subject].get("unread") for subject in wanted if subject in kept)
    if failures and not answered and not readable:
        return {"ok": False, "records": [], "error": failures[0]}
    return {"ok": True, "records": [kept[subject] for subject in wanted if subject in kept]}


def _registries(addresses, domains, allocations, registrations, now, force) -> dict[str, Any]:
    return {
        kind: report
        for kind, report in (
            (
                ADDRESS_KIND,
                _report(
                    ADDRESS_KIND,
                    "address",
                    wanted_addresses() if addresses is None else addresses,
                    lambda address: read_address(address, allocations),
                    now,
                    force=force,
                ),
            ),
            (
                DOMAIN_KIND,
                _report(
                    DOMAIN_KIND,
                    "domain",
                    wanted_domains() if domains is None else domains,
                    lambda domain: read_domain(domain, registrations),
                    now,
                    force=force,
                ),
            ),
        )
        if report is not None
    }


def _held_findings() -> dict[tuple[str, str, str], Mapping[str, Any]]:
    from .facts import inventory_records

    return {
        (str(item.get("id", "")), str(item.get("package", "")), str(item.get("installed", ""))): item
        for _snapshot, record in inventory_records(VULNERABILITY_KIND)
        for item in record.get("findings") or ()
    }


def _images(now: datetime, force: bool, principal: Principal) -> list[Any]:
    """Every image reading, in the order each depends on the one before:
    tags name the digests, a digest's provenance can name the source, and
    its package list is what OSV is asked about. Each is stored before the
    next is chosen."""

    from .inventory import record_inventory

    images = wanted_images()
    stages = (
        lambda: _report(IMAGE_KIND, "image", images, lambda name: read_image(name, images[name]), now, force=force),
        lambda: _report(DIGEST_KIND, "digest", wanted_digests(), read_digest, now),
        lambda: _report(
            UPSTREAM_KIND, "repository", wanted_upstreams(), read_upstream, now, UPSTREAMS_PER_REFRESH, force=force
        ),
        lambda: _report(
            VULNERABILITY_KIND,
            "digest",
            wanted_vulnerabilities(),
            vulnerability_reader(_held_findings()),
            now,
            force=force,
        ),
    )
    recorded: list[Any] = []
    for kind, stage in zip((IMAGE_KIND, DIGEST_KIND, UPSTREAM_KIND, VULNERABILITY_KIND), stages, strict=False):
        report = stage()
        if report is not None:
            recorded.extend(record_inventory({kind: report}, principal=principal).get("recorded") or ())
    return recorded


def refresh(
    *,
    principal: Principal,
    addresses: Iterable[str] | None = None,
    domains: Iterable[str] | None = None,
    allocations: Callable[..., dict] = registry,
    registrations: Callable[..., dict] = domain_registry,
    force: bool = False,
) -> dict[str, Any]:
    """Read what is due from the public registries and store it.

    Nothing is read that is not due, so a run with nothing new costs no
    request at all. ``force`` reads every subject again, except a digest's
    attestations, which cannot have changed.
    """

    from .inventory import record_inventory

    principal.require(Capability.LOOK_UP_PUBLIC_RECORDS)
    now = timezone.now()
    kinds = (ADDRESS_KIND, DOMAIN_KIND)
    payload: dict[str, Any] = {}
    if _configured():
        payload.update(_registries(addresses, domains, allocations, registrations, now, force))
    else:
        payload.update({kind: {"ok": True, "records": [], "connected": False} for kind in kinds})
    recorded = _images(now, force, principal)
    if not payload:
        return {"ok": True, "recorded": recorded}
    result = record_inventory(payload, principal=principal)
    return {**result, "recorded": recorded + list(result.get("recorded") or ())}


def registry_due() -> bool:
    """Whether a sweep found an image, or a digest of one, that HQ has not
    read. Cheap enough to ask on every sweep: two stored readings, no join."""

    from hq.domains.control_plane.observations.portainer import IMAGE_KIND as PULLED_KIND

    from .facts import inventory_records
    from .images import ImageRef

    wanted = wanted_images()
    held_images = {str(record.get("image")) for _snapshot, record in inventory_records(IMAGE_KIND)}
    if set(wanted) - held_images:
        return True
    running = {
        f"{image.name}@{image.digest}"
        for references in wanted.values()
        for image in (ImageRef.parse(reference) for reference in references)
        if image is not None and image.digest
    }
    for _snapshot, pulled in inventory_records(PULLED_KIND):
        if pulled.get("containers"):
            running.update(
                f"{image.name}@{image.digest}"
                for image in (ImageRef.parse(str(named)) for named in pulled.get("digests") or ())
                if image is not None and image.digest and image.name in wanted
            )
    held = {str(record.get("digest")) for _snapshot, record in inventory_records(DIGEST_KIND)}
    return bool(running - held)


# The HQ-side reader for each reading HQ takes itself; see ``read_by``.
READERS: dict[str, Callable[..., dict[str, Any]]] = {
    ADDRESS_KIND: read_address,
    DOMAIN_KIND: read_domain,
    IMAGE_KIND: read_image,
    UPSTREAM_KIND: read_upstream,
    DIGEST_KIND: read_digest,
    VULNERABILITY_KIND: read_vulnerabilities,
}

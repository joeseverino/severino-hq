"""Readings HQ takes itself from the keyless public registries.

RDAP, an image registry's public tags and the attestations published beside an
image, the releases and advisories an image's own repository publishes, and
OSV's vulnerability database need no credential, so HQ reads them rather than a
controller, after a page rather than during one, and stores them through the
same ingest as a sweep.
"""

from typing import Any

from ..names import normalized_hostname, organisation_name
from .contract import ObservationRecord, ObservationSpec

ADDRESS_KIND = "registry.address"
DOMAIN_KIND = "registry.domain"
IMAGE_KIND = "registry.image"
UPSTREAM_KIND = "registry.upstream"
DIGEST_KIND = "registry.digest"
VULNERABILITY_KIND = "registry.vulnerabilities"


class AddressHolderRecord(ObservationRecord):
    address: str
    # The organisation the allocation names, and the network's own name.
    organisation: str = ""
    network: str = ""
    handle: str = ""
    country: str = ""
    range: str = ""
    read_at: str = ""
    unread: str = ""


class DomainRegistrationRecord(ObservationRecord):
    domain: str
    registrar: str = ""
    registered_at: str = ""
    expires_at: str = ""
    status: tuple[str, ...] = ()
    read_at: str = ""
    unread: str = ""


class PublishedImageRecord(ObservationRecord):
    # ``registry/repository``: every tag of one image.
    image: str
    # The tags that carry a version, which are the only ones a version is
    # compared with.
    tags: tuple[str, ...] = ()
    # ``{tag: sha256:…}`` for each running tag and the newest tag of its shape:
    # what an upgrade would pin, and whether a running tag has moved since.
    digests: dict[str, str] = {}
    # Tags whose digest could not be read, which makes the image due again.
    unresolved: tuple[str, ...] = ()
    # What the image says about itself in its build labels: the repository it
    # is built from, the commit and the version.
    source: str = ""
    revision: str = ""
    version: str = ""
    read_at: str = ""
    unread: str = ""


class UpstreamRecord(ObservationRecord):
    # ``owner/repository`` on GitHub, as an image names its source.
    repository: str
    url: str = ""
    # Newest first: ``{tag, url, published_at}``, prereleases left out.
    releases: tuple[dict[str, Any], ...] = ()
    # Published security advisories: ``{id, severity, summary, url,
    # published_at, vulnerabilities: [[vulnerable range, patched versions]]}``.
    advisories: tuple[dict[str, Any], ...] = ()
    read_at: str = ""
    unread: str = ""


class ProvenanceRecord(ObservationRecord):
    # The in-toto predicate the publisher attached, as it states it: unsigned,
    # so a statement of how it was built rather than proof of it.
    format: str = ""
    source: str = ""
    revision: str = ""
    builder: str = ""
    finished_at: str = ""
    # What it was built on, ``pkg:docker/…`` with its digest.
    materials: tuple[str, ...] = ()


class DigestRecord(ObservationRecord):
    # ``registry/repository@sha256:…``: one image at one digest, which never
    # changes, so this is read once.
    digest: str
    image: str = ""
    # The linux/amd64 manifest the attestations are about.
    platform_digest: str = ""
    # Package URLs from the SBOM the publisher attached, when it attached one.
    packages: tuple[str, ...] = ()
    sbom: str = ""
    provenance: ProvenanceRecord | None = None
    read_at: str = ""
    unread: str = ""


class VulnerabilityFinding(ObservationRecord):
    id: str
    package: str = ""
    installed: str = ""
    # Versions of the package the advisory names as fixed, when it names any.
    fixed: tuple[str, ...] = ()
    severity: str = ""
    summary: str = ""
    aliases: tuple[str, ...] = ()
    url: str = ""
    modified: str = ""


class VulnerabilitiesRecord(ObservationRecord):
    # The digest the packages were read from, as ``DigestRecord`` names it.
    digest: str
    # How many packages were checked, and what matched.
    checked: int = 0
    findings: tuple[VulnerabilityFinding, ...] = ()
    # Ids whose detail is still to be read, which makes the digest due again.
    unresolved: tuple[str, ...] = ()
    read_at: str = ""
    unread: str = ""


def _address(record) -> tuple[str, ...]:
    address = str(record.get("address", "") or "").strip()
    return (address,) if address else ()


def _domain(record) -> tuple[str, ...]:
    domain = normalized_hostname(record.get("domain"))
    return (domain,) if domain else ()


OBSERVATIONS: tuple[ObservationSpec, ...] = (
    ObservationSpec(
        ADDRESS_KIND,
        "rdap",
        "Address owner",
        AddressHolderRecord,
        addresses=_address,
        title=lambda record: organisation_name(str(record.get("organisation") or record.get("network") or "")),
        relation="On the network of",
        facet="network",
        read_by="hq",
    ),
    ObservationSpec(
        DOMAIN_KIND,
        "rdap",
        "Domain registration",
        DomainRegistrationRecord,
        hostnames=_domain,
        title=lambda record: organisation_name(str(record.get("registrar") or "")),
        relation="Registered through",
        facet="registration",
        expires=lambda record: str(record.get("expires_at", "")),
        read_by="hq",
    ),
    ObservationSpec(
        IMAGE_KIND,
        "image_registry",
        "Published image",
        PublishedImageRecord,
        title=lambda record: str(record.get("image", "")),
        relation="Published as",
        read_by="hq",
    ),
    ObservationSpec(
        UPSTREAM_KIND,
        "upstream",
        "Upstream releases",
        UpstreamRecord,
        title=lambda record: str(record.get("repository", "")),
        relation="Built from",
        read_by="hq",
    ),
    ObservationSpec(
        DIGEST_KIND,
        "image_registry",
        "Image signatures",
        DigestRecord,
        title=lambda record: str(record.get("digest", "")),
        relation="Attested as",
        read_by="hq",
    ),
    ObservationSpec(
        VULNERABILITY_KIND,
        "osv",
        "Known vulnerabilities",
        VulnerabilitiesRecord,
        title=lambda record: str(record.get("digest", "")),
        relation="Checked against",
        read_by="hq",
    ),
)

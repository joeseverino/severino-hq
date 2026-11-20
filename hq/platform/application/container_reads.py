"""Every running container as a registered read resource.

The containers page's own answer (``application.containers``), projected to
JSON, so an agent asks the same question the page does and gets the same
answer: what runs where, whether it is current and safe, how it is run, and
what it would take to change it. Nothing here derives a fact.
"""

from collections.abc import Callable
from typing import Any

from .integration_specs import ListRead, ResourceSpec, typed_keys
from .projection import iso, page_of, projection_scope
from .record_errors import RecordNotFound
from .resources import BoundedQuery
from .security import Capability
lazy from .container_records import ContainerRecord, ContainerRow, RepositoryPart, UpgradeRecord, UpgradeRow, UsagePart
lazy from .containers import containers
lazy from .derived_reads import serialize_measurement
lazy from .record_saves import row
lazy from .upgrades import plans


def _repository(repo: Any) -> RepositoryPart | None:
    """What the GitHub App says about the repository that builds this image:
    what production runs, where the default branch is, and what is open."""

    if repo is None:
        return None
    deploy = repo.production or {}
    head = repo.head or {}
    return {
        "name": repo.name,
        "url": repo.url,
        "default_branch": repo.default_branch,
        "head": head.get("sha", ""),
        "deployed": deploy.get("sha", ""),
        "undeployed": bool(repo.undeployed),
        "verified": repo.production_verified,
        "open_pull_requests": [
            {
                "number": pull.get("number"),
                "title": pull.get("title", ""),
                "url": pull.get("url", ""),
                "draft": bool(pull.get("draft")),
            }
            for pull in repo.pull_requests or ()
        ],
    }


def _supply_chain(standing: Any) -> dict[str, Any]:
    """What its publisher attached to the digest that runs, and what OSV knows
    against those packages, fixed versions included."""

    provenance = standing.provenance or {}
    return {
        "source": standing.upstream,
        "source_known_by": standing.source_from,
        "provenance": (
            {key: provenance.get(key, "") for key in ("format", "revision", "builder")}
            | {
                "built_at": iso(standing.built_at),
                "built_on": list(standing.built_on),
                "commit_url": standing.commit_url,
            }
            if provenance
            else None
        ),
        "packages": standing.packages,
        "sbom": str((standing.attested or {}).get("sbom", "") or ""),
        "attestations_unread": str((standing.attested or {}).get("unread", "") or "")
        if standing.attested
        else "not read yet",
        "vulnerabilities_read_at": iso(standing.checked_at),
        "vulnerabilities": [
            {key: finding.get(key, "") for key in ("id", "package", "installed", "severity", "summary", "url")}
            | {"fixed": list(finding.get("fixed") or ())}
            for finding in standing.findings
        ],
        "urgent": [finding.get("id", "") for finding in standing.urgent],
    }


def _usage(usage: Any) -> UsagePart | None:
    """What it uses beside its limits, when its sweep sampled it: bytes and
    how they were counted, CPU as a share of one core since the sweep before."""

    if usage is None:
        return None
    return {
        "memory_bytes": usage.memory,
        "memory_counted_as": usage.memory_basis,
        "memory_limit_bytes": usage.memory_limit or None,
        "memory_of_limit_percent": usage.memory_of_limit,
        "cpu_percent_of_one_core": usage.cpu_percent,
        "cpu_limit": usage.cpu_limit or None,
        "read_at": iso(usage.read_at),
    }


def serialize_container(item: Any) -> ContainerRecord:
    standing = item.standing
    return {
        "address": item.address,
        "machine": item.machine.name,
        "name": item.running.name,
        "compose_project": item.running.stack,
        "url": item.url,
        "state": item.running.state,
        "status": item.running.status,
        "serves": list(item.serves),
        "image": {
            "reference": item.running.image,
            "registry": standing.image.registry,
            "repository": standing.image.repository,
            "tag": standing.tag,
            "digest": standing.digest,
            "image_id": standing.image_id,
            "pinned": standing.pinned,
            "built": iso(standing.built),
            "source": standing.source,
        },
        "standing": {
            "state": standing.state,
            "summary": standing.summary,
            "newer": list(standing.newer),
            "latest": standing.latest,
            "release": (
                {
                    "tag": standing.release.get("tag", ""),
                    "url": standing.release.get("url", ""),
                    "published": iso(standing.release.get("published")),
                }
                if standing.release
                else None
            ),
            "advisories": [
                {key: advisory.get(key, "") for key in ("id", "severity", "summary", "url")}
                | {"vulnerabilities": [list(pair) for pair in advisory.get("vulnerabilities") or ()]}
                for advisory in standing.advisories
            ],
            "unmatched_advisories": standing.unmatched,
            "unread": standing.unread,
            "registry_read_at": iso(standing.read_at),
            "upstream_read_at": iso(standing.upstream_read_at),
        },
        "project": (
            {"name": item.project.name, "slug": item.project.slug, "url": item.project.get_absolute_url()}
            if item.project is not None
            else None
        ),
        "repository": _repository(standing.repository),
        "runtime": dict(item.runtime) if item.runtime is not None else None,
        "usage": _usage(item.usage),
        "posture": serialize_measurement(item.posture),
        "supply_chain": _supply_chain(standing) | {"standard": serialize_measurement(item.supply_chain)},
    }


def _listed(found: Callable[[], list[Any]], serialize: Callable[[Any], dict[str, Any]], limit: int) -> dict[str, Any]:
    with projection_scope():
        page = page_of(found(), limit)
        return {**page, "items": [serialize(item) for item in page["items"]]}


def _one(found: Callable[[], list[Any]], address: str, serialize: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    """The item addressed ``machine:container``: a container, or its plan."""

    with projection_scope():
        item = next((each for each in found() if getattr(each, "container", each).address == address), None)
        if item is None:
            raise RecordNotFound("No container answers to that address.")
        return serialize(item)


def list_containers(*, limit: int = 50) -> dict[str, Any]:
    """Every running container, worst standing first, as the containers page lists them."""

    return _listed(containers, serialize_container, limit)


def get_container(address: str) -> dict[str, Any]:
    return _one(containers, address, serialize_container)


def serialize_plan(plan: Any) -> UpgradeRecord:
    def advisories(items):
        return [{key: item.get(key, "") for key in ("id", "severity", "summary", "url")} for item in items]

    ends = {
        "from": {"tag": plan.container.standing.tag, "digest": plan.container.standing.digest},
        "to": {"tag": plan.target_tag, "digest": plan.target_digest},
    }
    return row(
        plan,
        UpgradeRecord,
        address=plan.container.address,
        release=(
            {
                "tag": plan.release.get("tag", ""),
                "url": plan.release.get("url", ""),
                "published": iso(plan.release.get("published")),
            }
            if plan.release
            else None
        ),
        fixes=advisories(plan.fixes),
        introduces=advisories(plan.introduces),
        data=[dict(mount) for mount in plan.data],
        **ends,
    )


def list_upgrades(*, limit: int = 50) -> dict[str, Any]:
    """Every container something newer is published for, and what upgrading it would take."""

    return _listed(plans, serialize_plan, limit)


def get_upgrade(address: str) -> dict[str, Any]:
    return _one(plans, address, serialize_plan)


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "containers",
            "Containers",
            "Every running container: its image and whether it is current and safe, its runtime, "
            "memory and CPU, and its posture.",
            Capability.READ,
            listing=ListRead(list_containers, BoundedQuery, ContainerRecord, fields=typed_keys(ContainerRow)),
            detail_handler=get_container,
            identifier="address",
            web_route="control_plane:containers",
            record_type=ContainerRecord,
        ),
        ResourceSpec(
            "upgrades",
            "Upgrades",
            "Every running container with a newer release: target digest, fixes, data, verification, "
            "blockers and steps. Read-only.",
            Capability.READ,
            listing=ListRead(list_upgrades, BoundedQuery, UpgradeRecord, fields=typed_keys(UpgradeRow)),
            detail_handler=get_upgrade,
            identifier="address",
            web_route="control_plane:containers",
            record_type=UpgradeRecord,
        ),
    )

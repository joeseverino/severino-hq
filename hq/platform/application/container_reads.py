"""Every running container, its upgrade and each open update of its app, as
registered read resources.

The containers page's own answer (``application.containers``), projected to
JSON, so an agent asks the same question the page does and gets the same
answer: what runs where, whether it is current and safe, how it is run, and
what it would take to change it. Nothing here derives a fact.
"""

from collections.abc import Callable
from typing import Any

from .integration_specs import DetailRead, ListRead, ResourceSpec, typed_keys
from .projection import iso, page_of, projection_scope
from .record_errors import one_of
from .resources import BoundedQuery
from .security import Capability
lazy from .app_update_approval import approval_of
lazy from .app_updates import MergeStanding, app_updates
lazy from .container_records import (
    AppUpdateRecord,
    AppUpdateRow,
    ContainerRecord,
    ContainerRow,
    MergePart,
    RepositoryPart,
    UpgradeRecord,
    UpgradeRow,
    UsagePart,
)
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
            for pull in repo.pull_requests
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
        addressed = one_of(
            found(),
            lambda each: getattr(each, "container", each).address == address,
            "No container answers to that address.",
        )
        return serialize(addressed)


def list_containers(*, limit: int = 50) -> dict[str, Any]:
    """Every running container, worst standing first, as the containers page lists them."""

    return _listed(containers, serialize_container, limit)


def get_container(address: str) -> dict[str, Any]:
    return _one(containers, address, serialize_container)


def _advisories(items) -> list[dict[str, Any]]:
    return [{key: item.get(key, "") for key in ("id", "severity", "summary", "url")} for item in items]


def serialize_plan(plan: Any) -> UpgradeRecord:
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
        fixes=_advisories(plan.fixes),
        introduces=_advisories(plan.introduces),
        data=[dict(mount) for mount in plan.data],
        **ends,
    )


def list_upgrades(*, limit: int = 50) -> dict[str, Any]:
    """Every container something newer is published for, and what upgrading it would take."""

    return _listed(plans, serialize_plan, limit)


def get_upgrade(address: str) -> dict[str, Any]:
    return _one(plans, address, serialize_plan)


def _key(update: Any) -> str:
    return f"{update.repository.replace('/', ':')}:{update.number}:{update.app}"


def _image(image: Any) -> str:
    return f"{image.name}:{image.tag}" if image.tag else image.name


def serialize_app_update(update: Any) -> AppUpdateRecord:
    approval = approval_of(update)
    return row(
        update,
        AppUpdateRecord,
        key=_key(update),
        containers=[item.address for item in update.containers],
        pins=[
            {
                "service": pin.service,
                "before": _image(pin.before) if pin.before else None,
                "after": _image(pin.after),
                "digest": pin.digest,
                "scanned": pin.scanned,
            }
            for pin in update.pins
        ],
        checks={
            **{key: update.checks.get(key, default) for key, default in _CHECKS.items()},
            "why": update.checks_said.why,
        },
        fixes=_advisories(update.fixes),
        brings=_advisories(update.brings),
        merge=_merge(approval.standing) if approval.standing else None,
        held=approval.held,
    )


def _merge(standing: MergeStanding) -> MergePart:
    return {
        "words": standing.word,
        "tone": standing.tone,
        "run_url": standing.run_url,
        "taken": standing.taken,
        "lines": list(standing.lines),
    }


# What a head's checks say before any ran.
_CHECKS: dict[str, Any] = {"state": "", "total": 0, "failing": [], "running": 0}


def list_app_updates(*, limit: int = 50) -> dict[str, Any]:
    """Every open update of an app's Compose pins, and what it would move."""

    return _listed(lambda: list(app_updates()), serialize_app_update, limit)


def get_app_update(key: str) -> dict[str, Any]:
    with projection_scope():
        return serialize_app_update(
            one_of(app_updates(), lambda update: _key(update) == key, "No open app update answers to that key.")
        )


def resources() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            "containers",
            "Containers",
            "Every running container: its image and whether it is current and safe, its runtime, "
            "memory and CPU, and its posture.",
            Capability.READ,
            listing=ListRead(list_containers, BoundedQuery, ContainerRecord, fields=typed_keys(ContainerRow)),
            detail=DetailRead(get_container, ContainerRecord),
            identifier="address",
            web_route="control_plane:containers",
        ),
        ResourceSpec(
            "upgrades",
            "Upgrades",
            "Every running container with a newer release: target digest, fixes, data, verification, "
            "blockers and steps. Read-only.",
            Capability.READ,
            listing=ListRead(list_upgrades, BoundedQuery, UpgradeRecord, fields=typed_keys(UpgradeRow)),
            detail=DetailRead(get_upgrade, UpgradeRecord),
            identifier="address",
            web_route="control_plane:containers",
        ),
        ResourceSpec(
            "app_updates",
            "App updates",
            "Every open Renovate pull request that moves an app's Compose pins: each pin before and after, "
            "the containers it moves, its checks, and what it fixes and brings. Read-only.",
            Capability.READ,
            listing=ListRead(list_app_updates, BoundedQuery, AppUpdateRecord, fields=typed_keys(AppUpdateRow)),
            detail=DetailRead(get_app_update, AppUpdateRecord),
            identifier="key",
            web_route="control_plane:containers",
        ),
    )

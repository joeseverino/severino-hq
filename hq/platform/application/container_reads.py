"""Every running container as a registered read resource.

The containers page's own answer (``application.containers``), projected to
JSON, so an agent asks the same question the page does and gets the same
answer: what runs where, whether it is current and safe, how it is run, and
what it would take to change it. Nothing here derives a fact.
"""

from collections.abc import Callable
from typing import Any

from .projection import page_size, projection_scope


class NotFoundError(ValueError):
    """No running container answers to the address."""


def _moment(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _repository(repo: Any) -> dict[str, Any] | None:
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
        "undeployed": bool(head.get("sha") and deploy.get("sha") and head.get("sha") != deploy.get("sha")),
        "verified": repo.production_verified,
        "open_pull_requests": [
            {"number": pull.get("number"), "title": pull.get("title", ""), "url": pull.get("url", ""), "draft": bool(pull.get("draft"))}
            for pull in repo.pull_requests or ()
        ],
    }


def _posture(posture: Any) -> dict[str, Any]:
    return {
        "met": posture.met,
        "measured": posture.measured,
        "checks": [{"id": result.check.id, "label": result.check.label, "state": result.state} for result in posture.results],
        "unmet": [
            {"id": result.check.id, "label": result.check.label, "serious": result.check.serious,
             "why": result.check.why, "fix": result.check.fix}
            for result in posture.unmet
        ],
        "by_design": [
            {"id": result.check.id, "label": result.check.label, "reason": result.reason}
            for result in posture.intended
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
            | {"built_at": _moment(standing.built_at), "built_on": list(standing.built_on), "commit_url": standing.commit_url}
            if provenance
            else None
        ),
        "packages": standing.packages,
        "sbom": str((standing.attested or {}).get("sbom", "") or ""),
        "attestations_unread": str((standing.attested or {}).get("unread", "") or "") if standing.attested else "not read yet",
        "vulnerabilities_read_at": _moment(standing.checked_at),
        "vulnerabilities": [
            {key: finding.get(key, "") for key in ("id", "package", "installed", "severity", "summary", "url")}
            | {"fixed": list(finding.get("fixed") or ())}
            for finding in standing.findings
        ],
        "urgent": [finding.get("id", "") for finding in standing.urgent],
    }


def serialize_container(item: Any) -> dict[str, Any]:
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
            "built": _moment(standing.built),
            "source": standing.source,
        },
        "standing": {
            "state": standing.state,
            "summary": standing.summary,
            "newer": list(standing.newer),
            "latest": standing.latest,
            "release": (
                {"tag": standing.release.get("tag", ""), "url": standing.release.get("url", ""), "published": _moment(standing.release.get("published"))}
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
            "registry_read_at": _moment(standing.read_at),
            "upstream_read_at": _moment(standing.upstream_read_at),
        },
        "project": (
            {"name": item.project.name, "slug": item.project.slug, "url": item.project.get_absolute_url()}
            if item.project is not None
            else None
        ),
        "repository": _repository(standing.repository),
        "runtime": dict(item.runtime) if item.runtime is not None else None,
        "posture": _posture(item.posture),
        "supply_chain": _supply_chain(standing) | {"standard": _posture(item.supply_chain)},
    }


def _listed(found: Callable[[], list[Any]], serialize: Callable[[Any], dict[str, Any]], limit: int) -> dict[str, Any]:
    with projection_scope():
        items = [serialize(item) for item in found()[: page_size(limit)]]
    return {"items": items, "count": len(items)}


def _one(found: Callable[[], list[Any]], address: str, serialize: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    """The item addressed ``machine:container``: a container, or its plan."""

    with projection_scope():
        item = next((each for each in found() if getattr(each, "container", each).address == address), None)
        if item is None:
            raise NotFoundError(address)
        return serialize(item)


def list_containers(*, limit: int = 50) -> dict[str, Any]:
    """Every running container, worst standing first, as the containers page lists them."""

    from .containers import containers

    return _listed(containers, serialize_container, limit)


def get_container(address: str) -> dict[str, Any]:
    from .containers import containers

    return _one(containers, address, serialize_container)


def serialize_plan(plan: Any) -> dict[str, Any]:
    def blockers(items):
        return [{"id": item.id, "reason": item.reason} for item in items]

    def advisories(items):
        return [{key: item.get(key, "") for key in ("id", "severity", "summary", "url")} for item in items]

    return {
        "address": plan.container.address,
        "viable": plan.viable,
        "risk": plan.risk,
        "change": plan.change,
        "from": {"tag": plan.container.standing.tag, "digest": plan.container.standing.digest},
        "to": {"tag": plan.target_tag, "digest": plan.target_digest},
        "release": (
            {"tag": plan.release.get("tag", ""), "url": plan.release.get("url", ""), "published": _moment(plan.release.get("published"))}
            if plan.release
            else None
        ),
        "fixes": advisories(plan.fixes),
        "introduces": advisories(plan.introduces),
        "stateful": plan.stateful,
        "data": [dict(mount) for mount in plan.data],
        "verified_by": list(plan.verified_by),
        "blockers": blockers(plan.blockers),
        "not_automatic": blockers(plan.not_automatic),
        "steps": [{"id": step.id, "label": step.label, "detail": step.detail} for step in plan.steps],
    }


def list_upgrades(*, limit: int = 50) -> dict[str, Any]:
    """Every container something newer is published for, and what upgrading it would take."""

    from .upgrades import plans

    return _listed(plans, serialize_plan, limit)


def get_upgrade(address: str) -> dict[str, Any]:
    from .upgrades import plans

    return _one(plans, address, serialize_plan)

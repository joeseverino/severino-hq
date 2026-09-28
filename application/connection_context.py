"""Everything HQ derives about its connections, for every adapter.

The connections page, the HTTP API, MCP, the CLI and the SDK read this one
projection: each connection with what its credential can see, how fresh each
reading is, what the provider refused and the credential fix, its last
activity and whether a read was asked for; the summary counts; the security
posture; and HQ's own request path. None of them derives a fact the others
cannot return.

The posture has two halves. The estate half is the same for every caller. The
request half (how the request being answered reached HQ) exists only where
there is a request, and is returned under ``request``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from core.audit import last_activity
from core.models import AuditLog

from .action_links import read_now_link
from .cadence import forced_reads
from .connection_contracts import ConnectionInstance
from .connection_reach import ConnectionReach, connection_reach
from .connection_security import (
    REQUEST_CONTROLS,
    ConnectionSecurityPosture,
    connection_security_posture,
    observed_connection_controls,
)
from .connections import (
    CONTROLLER_CONNECTIONS,
    ConnectionGroup,
    ConnectionView,
    connection_catalog,
    serialize_connection,
)
from .credential_mint import CredentialFix, credential_fixes
from .credential_sight import ProviderSight, sight_by_connection
from .derived_reads import NotFoundError, serialize_path, serialize_provider_sight
from .freshness import freshness
from .hq_self import hq_hostnames
from .paths import hq_path
from .path_model import ServicePath
from .request_path import joined
from .projection import projection_scope
from .security import Principal
from .workflow_contracts import ActionLink


@dataclass(frozen=True)
class ConnectionRow:
    """One connection, and everything the page says beside it."""

    group: ConnectionGroup
    connection: ConnectionView
    sight: ProviderSight | None
    fix: CredentialFix | None
    last_event: AuditLog | None
    read_now: ActionLink | None
    read_requested_at: datetime | None
    reach: ConnectionReach | None = None

    @property
    def more_scope(self) -> tuple[str, ...]:
        return self.sight.more_scope if self.sight is not None else ()

    @property
    def instance(self) -> ConnectionInstance:
        return self.connection.instance

    def as_dict(self) -> dict[str, Any]:
        sight = self.sight
        return {
            **serialize_connection(self.connection),
            "family": self.group.spec.name,
            "family_label": self.group.spec.label,
            "secret_store": self.group.spec.secret_store or None,
            "connection_ref": self.instance.connection_ref or None,
            "reach": self.reach.as_dict() if self.reach is not None else None,
            "sight": _sight(sight) if sight is not None else None,
            "would_also_see": list(self.more_scope),
            "credential_fix": _fix(self.fix) if self.fix is not None else None,
            "last_activity": (
                {
                    "summary": self.last_event.summary,
                    "created_at": self.last_event.created_at.isoformat(),
                    "audit_id": self.last_event.pk,
                }
                if self.last_event is not None
                else None
            ),
            "read_now": asdict(self.read_now) if self.read_now else None,
            "read_requested_at": _moment(self.read_requested_at),
        }


def _moment(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _sight(sight: ProviderSight) -> dict[str, Any]:
    found = serialize_provider_sight(sight)
    for item, seen in zip(found["sights"], sight.sights, strict=True):
        aged = freshness(seen.kind, seen.observed_at)
        item["freshness"] = aged.state
        item["freshness_label"] = aged.label
    return found


def _fix(fix: CredentialFix) -> dict[str, Any]:
    return {
        "missing": list(fix.missing),
        "unseen": list(fix.unseen),
        "refused": fix.refused or None,
        "expires_at": _moment(fix.expires_at),
        "expiring": fix.expiring,
        "expired": fix.expired,
        "permissions": list(fix.permissions),
        "steps": [asdict(step) for step in fix.steps],
    }


def _control(control: Any) -> dict[str, str]:
    return asdict(control)


@dataclass(frozen=True)
class ConnectionsContext:
    groups: tuple[ConnectionGroup, ...]
    rows: tuple[ConnectionRow, ...]
    unconfigured: tuple[ConnectionGroup, ...]
    unconnected_providers: tuple[ProviderSight, ...]
    unlabelled: tuple[ConnectionView, ...]
    oldest: ConnectionInstance | None
    # The estate alone, the same for every caller.
    estate_posture: ConnectionSecurityPosture
    # With the request's admission joined in, where there is a request.
    posture: ConnectionSecurityPosture
    hq_path: ServicePath | None
    # ``hq_path`` from the caller's device, where there is a request.
    caller_path: ServicePath | None
    read_all: ActionLink | None
    reading_everything_since: datetime | None
    answers_request: bool

    @property
    def connection_count(self) -> int:
        return self.estate_posture.connection_count

    @property
    def attention_count(self) -> int:
        return self.estate_posture.attention_count

    @property
    def all_custodied(self) -> bool:
        posture = self.estate_posture
        return bool(posture.connection_count) and (
            posture.external_custody_count == posture.connection_count
        )

    def as_dict(self) -> dict[str, Any]:
        """The derived facts, for the API, MCP, CLI and SDK."""

        estate = self.estate_posture
        items = [row.as_dict() for row in self.rows]
        return {
            "items": items,
            "count": len(items),
            "summary": {
                "connections": estate.connection_count,
                "attention": estate.attention_count,
                "healthy": estate.healthy_count,
                "unverified": estate.unverified_count,
                "externally_custodied": estate.external_custody_count,
                "all_custodied": self.all_custodied,
                "oldest": (
                    {
                        "label": self.oldest.label,
                        "connection_ref": self.oldest.connection_ref or None,
                        "observed_at": _moment(self.oldest.observed_at),
                    }
                    if self.oldest is not None
                    else None
                ),
            },
            "not_connected": [
                {
                    "family": group.spec.name,
                    "label": group.spec.label,
                    "detail": group.spec.empty_message,
                }
                for group in self.unconfigured
            ]
            + [
                {
                    "provider": provider.provider,
                    "label": provider.label,
                    "sees": list(provider.sees),
                    "manages": list(provider.manages),
                    "requires": sorted(
                        {name for sight in provider.sights for name in sight.permissions}
                    ),
                }
                for provider in self.unconnected_providers
            ],
            "unclassified": [view.instance.label for view in self.unlabelled],
            "posture": {
                "state": estate.state,
                "summary": estate.summary,
                "controls": [_control(control) for control in estate.controls],
                "network_gate_enforced": estate.network_gate_enforced,
                "abilities": estate.ability_count,
                "dependencies": estate.dependency_count,
            },
            "hq_path": serialize_path(self.hq_path) if self.hq_path else None,
            "read_all": asdict(self.read_all) if self.read_all else None,
            "reading_everything_since": _moment(self.reading_everything_since),
            "request": self._request() if self.answers_request else None,
        }

    def _request(self) -> dict[str, Any]:
        posture = self.posture
        caller = self.caller_path.primary if self.caller_path else None
        return {
            "headline": posture.headline,
            "state": posture.state,
            "channel": posture.channel_label,
            "secure_transport": posture.secure_transport,
            "trusted_proxies": posture.trusted_proxy_count,
            "controls": [
                _control(control)
                for control in posture.controls
                if control.id in REQUEST_CONTROLS
            ],
            "caller": caller.hops[0].name if caller and caller.hops else None,
        }


def _pending_reads() -> tuple[dict[str, datetime], datetime | None]:
    """When each connection's read was asked for, and a pending read of everything."""

    by_ref: dict[str, datetime] = {}
    everything = None
    for read in forced_reads():
        if read.connection_ref:
            by_ref[read.connection_ref] = read.requested_at
        elif not read.kind:
            everything = read.requested_at
    return by_ref, everything


def _rows(
    groups: tuple[ConnectionGroup, ...],
    controller_refs: dict[str, str],
    principal: Principal,
    pending: dict[str, datetime],
) -> tuple[tuple[ConnectionRow, ...], tuple[ProviderSight, ...]]:
    sights, unconnected = sight_by_connection(controller_refs)
    fixes = credential_fixes()
    events = last_activity(
        connection.instance.connection_ref for group in groups for connection in group.connections
    )
    reach = connection_reach(
        (view.instance.connection_ref, view.instance.endpoint)
        for group in groups
        for view in group.connections
    )
    rows = []
    for group in groups:
        for connection in group.connections:
            ref = connection.instance.connection_ref
            fix = fixes.get(ref)
            rows.append(
                ConnectionRow(
                    group=group,
                    connection=connection,
                    sight=sights.get(ref),
                    fix=fix if fix is not None and fix.needed else None,
                    last_event=events.get(ref),
                    read_now=(
                        read_now_link(principal, connection_ref=ref)
                        if ref in controller_refs
                        else None
                    ),
                    read_requested_at=pending.get(ref),
                    reach=reach.get(ref),
                )
            )
    return tuple(rows), unconnected


def connections_context(*, principal: Principal, request: Any = None) -> ConnectionsContext:
    """Every fact HQ derives about its connections, derived once.

    ``request``, where there is one, adds how it reached HQ. Each family is
    shown only to a principal its spec permits.
    """

    with projection_scope():
        groups = connection_catalog(principal=principal)
        core = next(
            (group for group in groups if group.spec.name == CONTROLLER_CONNECTIONS), None
        )
        controller = core.connections if core else ()
        controller_refs = {
            view.instance.connection_ref: view.instance.kind
            for view in controller
            if view.instance.connection_ref
        }
        pending, everything = _pending_reads()
        rows, unconnected = _rows(groups, controller_refs, principal, pending)
        site = next(iter(hq_hostnames()), "")
        tailnet_policy, edge = observed_connection_controls(site)
        estate = connection_security_posture(groups, tailnet_policy=tailnet_policy, edge=edge)
        walked = hq_path()
        return ConnectionsContext(
            groups=groups,
            rows=rows,
            unconfigured=tuple(group for group in groups if not group.connections),
            unconnected_providers=unconnected,
            unlabelled=tuple(view for view in controller if view.instance.kind == "unclassified"),
            # The oldest, named, because the page's honesty depends on the
            # staler half: the newest would describe every row as current.
            oldest=min(
                (row.instance for row in rows if row.instance.observed_at),
                key=lambda instance: instance.observed_at,
                default=None,
            ),
            estate_posture=estate,
            posture=(
                connection_security_posture(
                    groups, request=request, tailnet_policy=tailnet_policy, edge=edge
                )
                if request is not None
                else estate
            ),
            hq_path=walked,
            caller_path=(
                joined(walked, request, None)
                if walked is not None and request is not None
                else None
            ),
            read_all=read_now_link(
                principal, every_connection=True, label="Read all now"
            ),
            reading_everything_since=everything,
            answers_request=request is not None,
        )


def list_connection_standing(*, principal: Principal) -> dict[str, Any]:
    """The connections page as one collection, for the registered read."""

    return connections_context(principal=principal).as_dict()


def get_connection_standing(connection_ref: str, *, principal: Principal) -> dict[str, Any]:
    """One connection as its row says it, by connection ref or instance id."""

    found = next(
        (
            item
            for item in connections_context(principal=principal).as_dict()["items"]
            if connection_ref in (item["connection_ref"], item["id"])
        ),
        None,
    )
    if found is None:
        raise NotFoundError(connection_ref)
    return found

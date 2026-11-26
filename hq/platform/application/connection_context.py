"""Everything HQ derives about its connections, for every adapter.

The connections page, the HTTP API, MCP, the CLI and the SDK read this one
projection: each connection with what its credential can see and how fresh,
what the provider refused and the credential fix, its last activity and any
read asked for; the summary counts; the security posture; and HQ's own request
path. None of them derives a fact the others cannot return.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from hq.platform.core.audit import last_activity
from hq.platform.core.models import AuditLog
lazy from hq.platform.application.routes import reverse
lazy from hq.platform.application.url_query import with_query

from . import record_saves
from .action_links import read_now_link
from .connection_catalog import (
    CONTROLLER_CONNECTIONS,
    ConnectionAbilityState,
    ConnectionGroup,
    ConnectionView,
    connection_catalog,
    serialize_ability,
)
from .connection_contracts import UNKNOWN_CREDENTIAL, ConnectionInstance
from .connection_lifecycle import WORKING, Lifecycle, standing
from .connection_reach import ConnectionReach, connection_reach
from .connection_security import ConnectionSecurityPosture, connection_security_posture
from .connection_setup import ProviderSetup, guide_url, setups
from .connections import connection_rows
from .credential_mint import CredentialFix, credential_fixes
from .credential_sight import PART_SOURCE, ProviderSight, Sight, fed_kinds, read_through, sight_by_connection
from .derived_reads import serialize_check, serialize_measurement, serialize_path, tally
from .entity_links import entity_link
from .freshness import freshness
from .json_schemas import as_written
from .moments import when
from .path_model import ServicePath
from .paths import hq_path
from .posture import estate_inputs, guide, posture
from .problems import problem_counts
from .projection import iso, page_of, projection_scope, read_once
from .read_requests import forced_reads
from .record_errors import RecordNotFound
from .request_connection import connection as request_connection
from .request_path import joined
from .security import Principal
from .standards import Measurement
from .tone import Tone
from .ui import counted
from .workflow_contracts import ActionLink
lazy from . import domains
lazy from .connection_records import (
    ConnectionStandingRecord,
    CredentialSightPart,
    LastActivityPart,
    ReadPart,
    SeenPart,
)


@dataclass(frozen=True, slots=True)
class Read:
    """One thing a connection reads, named once for every place the row lists it.

    ``sight`` is what the last sweep found of it, for a kind a sweep reads.
    ``ability`` is what grants it, when the connection declares one.
    """

    label: str
    sight: Sight | None = None
    ability: ConnectionAbilityState | None = None

    @property
    def whole(self) -> bool:
        """Whether its label is one name, kept on one line: the parts of another
        provider's kind are named together, and wrap between them."""

        return self.sight is None or self.sight.source != PART_SOURCE


@dataclass(frozen=True, slots=True)
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
    # The part of HQ that uses it, as its navigation names that part.
    used_for: str = ""
    # How many problems are open about it, and the queue showing only those.
    problems: int = 0
    # When its controller's report came with no connections at all.
    unreported_at: datetime | None = None

    @property
    def problems_url(self) -> str:
        about = entity_link("connection", self.instance.label).url
        return with_query(reverse("action_items"), {"about": about}) if self.problems else ""

    @property
    def reads(self) -> tuple[Read, ...]:
        """Everything the connection reads, by name.

        Every kind a sweep reads through this connection, as the sweep found it,
        and each reading ability no sweep makes (a lookup, an observer). A kind
        a sweep reads through another connection of the same provider is that
        connection's, so it is not listed here as an ability either.
        """

        granted = {state.ability.name: state for state in self.connection.abilities}
        sights = self.sight.sights if self.sight is not None else ()
        swept = set(fed_kinds(self.sight.provider)) if self.sight is not None else set()
        reads = (
            *(Read(sight.label, sight=sight, ability=granted.get(sight.kind)) for sight in sights),
            *(
                Read(state.ability.label, ability=state)
                for state in self.connection.abilities
                if not state.ability.effect.changes and state.ability.name not in swept
            ),
        )
        return tuple(sorted(reads, key=lambda read: read.label))

    @property
    def changes(self) -> tuple[ConnectionAbilityState, ...]:
        """What the connection's credential lets HQ change."""

        return tuple(state for state in self.connection.abilities if state.ability.effect.changes)

    @property
    def changes_only(self) -> bool:
        """Whether its credential reads nothing and only lets HQ change things:
        the Reads cell then says so and names what it changes, since an empty
        one beside "Working" reads as a reading gone missing."""

        return not self.reads and bool(self.changes)

    @property
    def more_scope(self) -> tuple[str, ...]:
        return self.sight.more_scope if self.sight is not None else ()

    @property
    def instance(self) -> ConnectionInstance:
        return self.connection.instance

    @property
    def lifecycle(self) -> Lifecycle:
        """The connection's lifecycle, with what its readings found folded in,
        so the state cannot disagree with the readings beside it."""

        return standing(
            Lifecycle(self.connection.lifecycle),
            unreported=self.unreported_at is not None,
            refused=self.sight is not None and bool(self.sight.credential_refusal),
        )

    @property
    def state_label(self) -> str:
        return self.lifecycle.label

    @property
    def _permissions_missing(self) -> int:
        if self.sight is None or self.lifecycle not in WORKING:
            return 0
        return len(self.sight.missing)

    @property
    def state_line(self) -> str:
        """The one line under the state word, or ""."""

        if self.lifecycle == Lifecycle.UNREPORTED:
            observed = self.instance.observed_at
            return f"Last reported {when(observed)}" if observed else ""
        if self.lifecycle != self.connection.lifecycle:
            return ""
        missing = self._permissions_missing
        if missing:
            return counted(missing, "permission missing", "permissions missing")
        return self.connection.state_line

    @property
    def state_line_quiet(self) -> bool:
        return not self._permissions_missing and self.connection.state_line_quiet

    @property
    def state_line_tone(self) -> str:
        """The tone of ``state_line``: a fault when a permission is missing,
        else none, and the line reads as context."""

        return Tone.SERIOUS if self._permissions_missing or self.connection.authority == "missing" else ""

    def as_dict(self) -> ConnectionStandingRecord:
        # Every ability and every sight is under ``reads`` or ``changes``, the
        # lists the page draws, so no client rebuilds them from the parts.
        connection, instance, sight, event = self.connection, self.instance, self.sight, self.last_event
        return record_saves.row(
            instance,
            ConnectionStandingRecord,
            credential_model=None if instance.credential_model == UNKNOWN_CREDENTIAL else instance.credential_model,
            # How it stands is its row's lifecycle, never the bare probe beneath it.
            status=self.lifecycle.tone,
            status_label=self.state_label,
            lifecycle=connection.lifecycle,
            authority=connection.authority,
            actions=connection.actions,
            reads=[_read(read) for read in self.reads],
            changes=[serialize_ability(state) for state in self.changes],
            family=self.group.spec.name,
            family_label=self.group.spec.label,
            used_for=self.used_for,
            open_problems=self.problems,
            secret_store=self.group.spec.secret_store,
            reach=self.reach.as_dict() if self.reach is not None else None,
            sight=_sight(sight) if sight is not None else None,
            would_also_see=self.more_scope,
            credential_fix=self.fix,
            last_activity=record_saves.row(event, LastActivityPart, audit_id=event.pk) if event is not None else None,
            read_now=self.read_now,
            read_requested_at=self.read_requested_at,
        )


def _sight(sight: ProviderSight) -> CredentialSightPart:
    return record_saves.row(sight, CredentialSightPart, tally=tally(sight))


def _read(read: Read) -> ReadPart:
    return {
        "label": read.label,
        "sight": _seen(read.sight) if read.sight is not None else None,
        "ability": serialize_ability(read.ability) if read.ability is not None else None,
    }


def _seen(sight: Sight) -> SeenPart:
    aged = freshness(sight.kind, sight.observed_at)
    return record_saves.row(sight, SeenPart, freshness=aged.state, freshness_label=aged.label)


@dataclass(frozen=True, slots=True)
class ConnectionsContext:
    groups: tuple[ConnectionGroup, ...]
    rows: tuple[ConnectionRow, ...]
    unconfigured: tuple[ConnectionGroup, ...]
    unconnected_providers: tuple[ProviderSight, ...]
    # How to connect each provider that has no connection yet: every one, on a
    # fresh install.
    setups: tuple[ProviderSetup, ...]
    # The same for each provider already connected: a second account of one is
    # still a connection to add, so the page always offers a way to add one.
    again: tuple[ProviderSetup, ...]
    unlabeled: tuple[ConnectionView, ...]
    oldest: ConnectionInstance | None
    # The estate alone, the same for every caller.
    estate_posture: ConnectionSecurityPosture
    # HQ's standard, read from the estate checks above.
    standard: Measurement
    # With the request's admission joined in, where there is a request.
    posture: ConnectionSecurityPosture
    hq_path: ServicePath | None
    # ``hq_path`` from the caller's device, where there is a request.
    caller_path: ServicePath | None
    read_all: ActionLink | None
    reading_everything_since: datetime | None
    answers_request: bool

    @property
    def to_connect(self) -> int:
        """Everything not yet connected: each service, and each family set up on its own."""

        return len(self.setups) + len(self.unconfigured)

    @property
    def guide(self) -> str:
        """Where adding a connection is written down."""

        return guide_url()

    @property
    def standard_guide(self) -> str:
        """Where HQ's standard is written down."""

        return guide()

    @property
    def connection_count(self) -> int:
        return self.estate_posture.connection_count

    @property
    def oldest_name(self) -> str:
        """What the page calls ``oldest``, as its row names it."""

        return entity_link("connection", self.oldest.label).label if self.oldest else ""

    @property
    def attention_count(self) -> int:
        return self.estate_posture.attention_count

    @property
    def attention_states(self) -> tuple[tuple[int, str], ...]:
        """(count, state) for each state that needs a person, in the rows' own words."""

        counts = Counter(row.state_label.lower() for row in self.rows if row.lifecycle.tone.acted_on)
        return tuple((count, label) for label, count in counts.items())

    @property
    def purposes(self) -> tuple[tuple[str, tuple[ConnectionRow, ...]], ...]:
        """The rows under what each is used for, in the rows' order."""

        found: dict[str, list[ConnectionRow]] = {}
        for row in self.rows:
            found.setdefault(row.used_for, []).append(row)
        return tuple((label, tuple(rows)) for label, rows in found.items())

    @property
    def all_custodied(self) -> bool:
        posture = self.estate_posture
        return bool(posture.connection_count) and (posture.external_custody_count == posture.connection_count)

    def as_dict(self) -> dict[str, Any]:
        """The derived facts, for the API, MCP, CLI and SDK."""

        estate = self.estate_posture
        return {
            **page_of(self.rows, each=ConnectionRow.as_dict),
            "summary": {
                "connections": estate.connection_count,
                Tone.ATTENTION: estate.attention_count,
                "healthy": estate.healthy_count,
                "unverified": estate.unverified_count,
                "externally_custodied": estate.external_custody_count,
                "all_custodied": self.all_custodied,
                "oldest": (
                    {
                        "label": self.oldest.label,
                        "connection_ref": self.oldest.connection_ref or None,
                        "observed_at": iso(self.oldest.observed_at),
                    }
                    if self.oldest is not None
                    else None
                ),
            },
            "standard": {"advanced": self.standard.enforced, **serialize_measurement(self.standard)},
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
                    "requires": sorted({name for sight in provider.sights for name in sight.permissions}),
                }
                for provider in self.unconnected_providers
            ],
            "unclassified": [view.instance.label for view in self.unlabeled],
            "posture": {
                "state": estate.state,
                "summary": estate.summary,
                "checks": [serialize_check(result) for result in estate.results],
                "network_gate_enforced": estate.network_gate_enforced,
                "abilities": estate.ability_count,
                "dependencies": estate.dependency_count,
            },
            "hq_path": serialize_path(self.hq_path) if self.hq_path else None,
            "read_all": as_written(self.read_all) if self.read_all else None,
            "reading_everything_since": iso(self.reading_everything_since),
            # The posture's request half (how the request being answered
            # reached HQ) exists only where there is a request; the estate
            # half above is the same for every caller.
            "request": self._request() if self.answers_request else None,
        }

    def _request(self) -> dict[str, Any]:
        posture = self.posture
        caller = self.caller_path.primary if self.caller_path else None
        return {
            "headline": posture.headline,
            "state": posture.state,
            "channel": posture.connection.channel.label,
            "secure_transport": posture.connection.facts.secure,
            "trusted_proxies": posture.trusted_proxy_count,
            "checks": [serialize_check(result) for result in posture.admission],
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
    unreported = {
        (row.controller_id, row.connection_ref): row.unreported_at for row in connection_rows() if row.unreported_at
    }
    fixes = credential_fixes()
    events = last_activity(connection.instance.connection_ref for group in groups for connection in group.connections)
    reach = connection_reach(
        (view.instance.connection_ref, view.instance.endpoint) for group in groups for view in group.connections
    )
    used_for = _used_for()
    open_problems = problem_counts(principal)
    rows = []
    for group in groups:
        found = []
        for connection in group.connections:
            ref = connection.instance.connection_ref
            fix = fixes.get(ref)
            found.append(
                ConnectionRow(
                    group=group,
                    connection=connection,
                    sight=sights.get(ref)
                    if ref in controller_refs
                    else read_through(connection.instance.kind, connection.instance.label),
                    fix=fix if fix is not None and fix.needed else None,
                    last_event=events.get(ref),
                    read_now=(read_now_link(principal, connection_refs=(ref,)) if ref in controller_refs else None),
                    read_requested_at=pending.get(ref),
                    reach=reach.get(ref),
                    used_for=used_for.get(group.spec.name, ""),
                    problems=open_problems.get(entity_link("connection", connection.instance.label).url, 0),
                    unreported_at=unreported.get((connection.instance.controller_id, ref)),
                )
            )
        # Within its group, a connection that needs a person comes first.
        rows.extend(sorted(found, key=lambda row: not row.lifecycle.tone.acted_on))
    # Under what each is used for, in the order the first of each appears: the
    # sort is stable, so the controller's connections still lead.
    order = {name: place for place, name in enumerate(dict.fromkeys(row.used_for for row in rows))}
    return tuple(sorted(rows, key=lambda row: order[row.used_for])), unconnected


def _used_for() -> dict[str, str]:
    """The part of HQ that declares each connection type, by the type's name.

    A part is named as the navigation names it: the group its pages sit under,
    else its own label. Derived from who declares the type, so an extension's
    connections fall under the extension without the host naming it.
    """

    def load() -> dict[str, str]:
        found: dict[str, str] = {}
        for domain in domains.all_domains():
            if domain.integration.connections is None:
                continue
            named = next((item.group for item in domain.navigation if item.group), "") or domain.label
            for spec in domain.integration.connections():
                found[spec.name] = named
        return found

    return read_once("connections.used_for", load)


def connections_context(*, principal: Principal, request: Any = None) -> ConnectionsContext:
    """Every fact HQ derives about its connections, derived once.

    ``request``, where there is one, adds how it reached HQ. Each family is
    shown only to a principal its spec permits.
    """

    with projection_scope():
        groups = connection_catalog(principal=principal)
        core = next((group for group in groups if group.spec.name == CONTROLLER_CONNECTIONS), None)
        controller = core.connections if core else ()
        controller_refs = {
            view.instance.connection_ref: view.instance.kind for view in controller if view.instance.connection_ref
        }
        pending, everything = _pending_reads()
        rows, unconnected = _rows(groups, controller_refs, principal, pending)
        every = setups()
        connected = frozenset(controller_refs.values())
        available = tuple(setup for setup in every if setup.provider not in connected)
        inputs = estate_inputs(groups)
        standard = posture(inputs)
        estate = connection_security_posture(inputs, standard)
        found = request_connection(request) if request is not None else None
        walked = hq_path()
        return ConnectionsContext(
            groups=groups,
            rows=rows,
            # With no controller connections yet, the setup list says what to add.
            unconfigured=tuple(
                group
                for group in groups
                if not group.connections and not (group.spec.name == CONTROLLER_CONNECTIONS and available)
            ),
            unconnected_providers=unconnected,
            setups=available,
            again=tuple(setup for setup in every if setup.provider in connected),
            unlabeled=tuple(view for view in controller if view.instance.kind == "unclassified"),
            # The oldest, named, because the page's honesty depends on the
            # staler half: the newest would describe every row as current.
            oldest=min(
                (row.instance for row in rows if row.instance.observed_at),
                key=lambda instance: instance.observed_at,
                default=None,
            ),
            estate_posture=estate,
            standard=standard,
            posture=(connection_security_posture(inputs, standard, connection=found) if found is not None else estate),
            hq_path=walked,
            caller_path=(joined(walked, request, found) if walked is not None and request is not None else None),
            read_all=read_now_link(principal, every_connection=True),
            reading_everything_since=everything,
            answers_request=request is not None,
        )


def list_connection_standing(*, principal: Principal) -> dict[str, Any]:
    """The connections page as one collection, for the registered read."""

    return connections_context(principal=principal).as_dict()


def get_connection_standing(connection_ref: str, *, principal: Principal) -> ConnectionStandingRecord:
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
        raise RecordNotFound("No connection has that reference.")
    return found

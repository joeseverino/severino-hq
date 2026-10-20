"""HQ's standard: the checks beyond what every HQ enforces (docs/Posture.md).

With ``SEVERINO_POSTURE=advanced`` each unmet check is also a finding.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.conf import settings

from hq.domains.control_plane.provider_adapters.tailscale import TAILNET_POLICY_KIND
from hq.platform.core.oidc import sso_only
lazy from hq.domains.control_plane.connection_kinds import CONNECTION_LABELS
lazy from hq.domains.control_plane.observations import OBSERVATIONS
lazy from hq.domains.control_plane.observations.host import FIREWALL_KIND, TAILNET_LOCK_KIND
lazy from hq.domains.control_plane.providers import PROVIDERS

from .ui import counted
lazy from . import cadence, tailnet
lazy from .connection_security import host_firewall_control, ingress_kinds, observed_connection_controls
lazy from .connection_setup import docs_url, setups
lazy from .connections import connection_rows
lazy from .derivations import present
lazy from .facts import stored_snapshots
lazy from .github_estate import REPOSITORY_KIND
lazy from .hq_context.release import repository
lazy from .hq_self import hq_hostnames
lazy from .routes import reverse

NOT_READ = "Not read yet"


@dataclass(frozen=True, slots=True)
class Reading:
    """``met`` is None when there is nothing to read it from."""

    met: bool | None
    evidence: str
    # Met, with a limit the reader should see: a login whose service offers
    # nothing narrower is the best it can be, not a clean pass.
    qualified: bool = False
    # What would let HQ read an unread check, and where that is done.
    remedy: str = ""
    remedy_url: str = ""


@dataclass(frozen=True, slots=True)
class Check:
    id: str
    label: str
    # The finding's title.
    unmet: str
    # Read after "Stops" in the check's sentence.
    stops: str
    # The panel's sentence when it is met.
    holds: str
    step: str
    read: Callable[[Inputs], Reading]


@dataclass(frozen=True, slots=True)
class Standing:
    check: Check
    reading: Reading
    # Under Advanced HQ Posture an unmet check is a fault; otherwise it is
    # an open item, shown quietly.
    enforced: bool = False

    @property
    def met(self) -> bool:
        return self.reading.met is True

    @property
    def state(self) -> str:
        if self.met and not self.reading.qualified:
            return "good"
        return "attention" if self.enforced and self.reading.met is False else "neutral"

    @property
    def icon(self) -> str:
        """A tick for met, a cross for a fault, a clock for not read yet: an
        unread check is waiting on a reading, not failing."""

        if self.met:
            return "check-circle"
        if self.reading.met is None:
            return "clock"
        return "x-circle" if self.state == "attention" else "circle"


@dataclass(frozen=True, slots=True)
class Posture:
    advanced: bool
    standings: tuple[Standing, ...]

    @property
    def met_count(self) -> int:
        return sum(standing.met for standing in self.standings)

    @property
    def unread_count(self) -> int:
        return sum(standing.reading.met is None for standing in self.standings)

    @property
    def count_line(self) -> str:
        """ "5 of 7 met · not met: Tailnet lock is on · 1 not read yet": a count
        names what it is short of, and an unread check is not counted as unmet."""

        parts = [f"{self.met_count} of {len(self.standings)} met"]
        if failing := [standing.check.label for standing in self.unmet if standing.reading.met is False]:
            parts.append(f"not met: {', '.join(failing)}")
        if self.unread_count:
            parts.append(f"{self.unread_count} not read yet")
        return " · ".join(parts)

    @property
    def unmet(self) -> tuple[Standing, ...]:
        return tuple(standing for standing in self.standings if not standing.met)

    def as_dict(self) -> dict[str, Any]:
        return {
            "advanced": self.advanced,
            "met": self.met_count,
            "unread": self.unread_count,
            "checks": [
                {
                    "id": standing.check.id,
                    "label": standing.check.label,
                    "met": standing.reading.met,
                    "evidence": standing.reading.evidence,
                    "qualified": standing.reading.qualified,
                    "stops": standing.check.stops,
                }
                for standing in self.standings
            ],
        }

    @property
    def guide(self) -> str:
        return docs_url("Posture.md")


def advanced() -> bool:
    return settings.SEVERINO_POSTURE == "advanced"


@dataclass(frozen=True, slots=True)
class Inputs:
    """The proxy's access-list control for HQ's name, and the connection families."""

    edge: Any
    groups: tuple[Any, ...]


def _from_control(control, kinds: tuple[str, ...]) -> Reading:
    """Evidence only when unmet: a met check's label says it."""

    met = {"good": True, "neutral": None, "attention": None}.get(control.state, False)
    if met is None:
        return _not_read(kinds, control.evidence)
    return Reading(met, "" if met else control.evidence)


def _readers(kinds: tuple[str, ...]) -> tuple[str, ...]:
    """The connection providers whose readings these kinds are, from their declarations."""

    found: list[str] = []
    for kind in kinds:
        if kind in OBSERVATIONS:
            found.append(OBSERVATIONS[kind].provider)
        elif kind in PROVIDERS:
            found.extend(PROVIDERS[kind].connection_providers)
    return tuple(dict.fromkeys(found))


def _not_read(kinds: tuple[str, ...], evidence: str = NOT_READ) -> Reading:
    """A check nothing has read, with what would read it: the connection to
    add, else the controller to run, else only that the reading is due."""

    if evidence == "Not read":
        evidence = NOT_READ
    connected = {row.provider for row in connection_rows()}
    readers = [provider for provider in _readers(kinds) if provider in CONNECTION_LABELS]
    if readers and not connected.intersection(readers):
        labels = " or ".join(CONNECTION_LABELS[provider] for provider in readers)
        return Reading(None, evidence, remedy=f"connect {labels}", remedy_url=_setup_url())
    if not cadence.controller_standing(present()).known:
        return Reading(None, evidence, remedy="run the controller", remedy_url=reverse("control_plane:controller"))
    return Reading(None, evidence)


def _setup_url() -> str:
    return reverse("control_plane:connections") + "#connection-setup-heading"


def _sign_in(_inputs) -> Reading:
    return Reading(True, "") if sso_only() else Reading(False, "Password sign-in installed")


def _edge(inputs) -> Reading:
    return _from_control(inputs.edge, ingress_kinds())


def _firewall(_inputs) -> Reading:
    return _from_control(
        host_firewall_control(next(iter(stored_snapshots().get(FIREWALL_KIND, ())), None)), (FIREWALL_KIND,)
    )


def _allows_everyone(policy) -> bool:

    grants = any(
        "*" in (grant.get("src") or ()) and "*" in (grant.get("dst") or ()) and "*" in (grant.get("ip") or ())
        for grant in policy.grants
        if isinstance(grant, dict)
    )
    acls = any(
        rule.get("action", "accept") == "accept" and "*" in (rule.get("src") or ()) and "*:*" in (rule.get("dst") or ())
        for rule in policy.acls
    )
    return grants or acls


def _tailnet_policy(_inputs) -> Reading:
    policy = tailnet.policy()
    if not policy.known and not policy.acls:
        return _not_read((TAILNET_POLICY_KIND,))
    if _allows_everyone(policy):
        return Reading(False, "An allow-all rule")
    return Reading(True, counted(len(policy.grants) + len(policy.acls), "rule", "rules"))


def _tailnet_lock(_inputs) -> Reading:
    lock = tailnet.policy().lock
    if not lock:
        # A read that failed keeps the last reading, so with none it says why.
        snapshot = next(iter(stored_snapshots().get(TAILNET_LOCK_KIND, ())), None)
        return _not_read((TAILNET_LOCK_KIND,), (snapshot.error if snapshot is not None else "") or NOT_READ)
    keys = lock.get("trusted_keys") or 0
    return Reading(True, counted(keys, "signing key", "signing keys")) if lock.get("enabled") else Reading(False, "Off")


def _scope(inputs) -> Reading:
    """Unmet only where the provider takes a scoped token (its setup lists
    permissions); a login has no scoped form."""

    scoped = {setup.provider for setup in setups() if setup.permissions}
    connections = tuple(connection for group in inputs.groups for connection in group.connections)
    broad = sorted(
        connection.instance.label
        for connection in connections
        if connection.instance.credential_model == "coarse" and connection.instance.kind in scoped
    )
    if broad:
        return Reading(False, f"Full account access: {', '.join(broad)}")
    if not any(connection.instance.credential_model not in ("none", "") for connection in connections):
        return Reading(None, "No connections yet", remedy="add a connection", remedy_url=_setup_url())
    if any(connection.instance.credential_model in ("", "unknown") for connection in connections):
        return Reading(None, NOT_READ)
    return _scoped_where_offered(connections, scoped)


def _scoped_where_offered(connections, scoped: set[str]) -> Reading:
    """Met, said as what was checked: the scoped tokens, and the logins whose
    service offers nothing narrower, by name, so a whole-account login is never
    read as a clean pass."""

    tokens = sum(connection.instance.kind in scoped for connection in connections)
    whole = sorted(
        connection.instance.label
        for connection in connections
        if connection.instance.credential_model == "coarse" and connection.instance.kind not in scoped
    )
    said = [counted(tokens, "scoped token", "scoped tokens")] if tokens else []
    if whole:
        said.append(f"nothing narrower offered for {', '.join(whole)}")
    evidence = ", ".join(said)
    return Reading(True, evidence[:1].upper() + evidence[1:], qualified=bool(whole))


def _approval(_inputs) -> Reading:
    found = repository(None)
    if found is None:
        return _not_read((REPOSITORY_KIND,))
    production = found.production_environment
    if production is None:
        return Reading(False, "No production environment")
    reviewers = production.get("reviewers") or ()
    return Reading(True, f"Reviewers: {', '.join(reviewers)}") if reviewers else Reading(False, "No reviewers")


STANDARD: tuple[Check, ...] = (
    Check(
        "edge",
        "The proxy lets in tailnet addresses only",
        "The proxy lets in more than the tailnet",
        "the proxy exposing HQ beyond the tailnet",
        "The proxy refuses any address outside the tailnet.",
        "Give HQ's proxy host an access list that allows the two tailnet ranges and denies everything else.",
        _edge,
    ),
    Check(
        "host-firewall",
        "The firewall requires the tailnet interface",
        "The firewall accepts HQ's port by address alone",
        "a forged tailnet address arriving another way",
        "The firewall drops a tailnet address that arrives on any other interface.",
        "Accept HQ's port only on the tailnet interface in the host firewall.",
        _firewall,
    ),
    Check(
        "tailnet-policy",
        "The tailnet policy has no allow-all rule",
        "The tailnet policy lets every device reach every device",
        "any device on the tailnet opening HQ",
        "A device on the tailnet can open only what the policy allows it.",
        "Replace the allow-all rule with grants that name who reaches HQ.",
        _tailnet_policy,
    ),
    Check(
        "tailnet-lock",
        "Tailnet lock is on",
        "Tailnet lock is off",
        "a device added to your tailnet without your key",
        "A device added to your tailnet without your key is refused by tailnet lock.",
        "Turn on tailnet lock, with signing keys on devices you hold.",
        _tailnet_lock,
    ),
    Check(
        "sign-in",
        "Sign-in is single sign-on only",
        "Password sign-in is installed",
        "a guessed or reused password",
        "Every session starts with single sign-on.",
        "Turn password sign-in off in HQ's settings once single sign-on works.",
        _sign_in,
    ),
    Check(
        "scope",
        "Each token is scoped where its service allows it",
        "A full-account token is used where a scoped one is offered",
        "a stolen token reaching the whole account",
        "A stolen token reaches only what its scopes allow.",
        "Replace the full-account token with a scoped one holding the permissions its connection lists.",
        _scope,
    ),
    Check(
        "approval",
        "Deploys wait for your approval",
        "Deploys do not wait for approval",
        "a merge reaching production before you see it",
        "Nothing reaches production until you approve it.",
        "Add yourself as a required reviewer on the repository's production environment.",
        _approval,
    ),
)


def posture(inputs: Inputs) -> Posture:
    enforced = advanced()
    return Posture(enforced, tuple(Standing(check, check.read(inputs), enforced) for check in STANDARD))


def estate_posture(groups) -> Posture:
    """For callers that have not read the proxy's control for HQ's name."""

    _policy, edge = observed_connection_controls(next(iter(hq_hostnames()), ""))
    return posture(Inputs(edge, tuple(groups)))

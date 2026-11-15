"""HQ's standard: the checks beyond what every HQ enforces (docs/Posture.md).

With ``SEVERINO_POSTURE=advanced`` each unmet check is also a finding.
"""

from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.utils.text import capfirst

from hq.domains.control_plane.provider_adapters.tailscale import TAILNET_POLICY_KIND
from hq.platform.core.oidc import sso_only
lazy from hq.domains.control_plane.connection_kinds import CONNECTION_LABELS
lazy from hq.domains.control_plane.observations import OBSERVATIONS
lazy from hq.domains.control_plane.observations.host import FIREWALL_KIND, TAILNET_LOCK_KIND
lazy from hq.domains.control_plane.providers import PROVIDERS, registry_label

from .connection_contracts import UNKNOWN_CREDENTIAL
from .standards import Check, Measurement, Reading, Standing, measure
from .ui import counted
lazy from . import cadence, tailnet
lazy from .connection_security import host_firewall_control, ingress_kinds, observed_connection_controls
lazy from .connection_setup import docs_url, setups
lazy from .connections import connection_rows
lazy from .derivations import present
lazy from .facts import stored_snapshots
lazy from .github_estate import REPOSITORY_KIND
lazy from .hq_context.release_part import repository
lazy from .hq_self import hq_hostnames
lazy from .integration_validation import required_capability_names
lazy from .resource_records import StandardPart
lazy from .routes import reverse


def advanced() -> bool:
    return settings.SEVERINO_POSTURE == "advanced"


@dataclass(frozen=True, slots=True)
class Inputs:
    """The proxy's access-list control for HQ's name, and the connection families."""

    edge: Any
    groups: tuple[Any, ...]


def _from_control(control, kinds: tuple[str, ...]) -> Reading:
    """Evidence only when unmet: a met check's label says it."""

    if control.state == "good":
        return Reading.met()
    if control.state in {"neutral", "attention"}:
        return _not_read(kinds, held=control.detail)
    return Reading.unmet(control.evidence)


def _readers(kinds: tuple[str, ...]) -> tuple[str, ...]:
    """The connection providers whose readings these kinds are, from their declarations."""

    found: list[str] = []
    for kind in kinds:
        if kind in OBSERVATIONS:
            found.append(OBSERVATIONS[kind].provider)
        elif kind in PROVIDERS:
            found.extend(PROVIDERS[kind].connection_providers)
    return tuple(dict.fromkeys(found))


def _not_read(kinds: tuple[str, ...], *, held: str = "") -> Reading:
    """A check HQ could not judge, and why, as its evidence: no connection
    reads it (and which to add), no controller has run, the last read failed,
    no read has arrived yet, or ``held``, what the reading held instead of an
    answer."""

    connected = {row.provider for row in connection_rows()}
    readers = [provider for provider in _readers(kinds) if provider in CONNECTION_LABELS]
    if readers and not connected.intersection(readers):
        labels = " or ".join(CONNECTION_LABELS[provider] for provider in readers)
        return Reading.unavailable(f"No {labels} connection reads it", fix=f"connect {labels}", fix_url=_setup_url())
    if not cadence.controller_standing(present()).known:
        return Reading.unavailable(
            "HQ has not read it", fix="run the controller", fix_url=reverse("control_plane:controller")
        )
    stored = stored_snapshots()
    rows = [row for kind in kinds for row in stored.get(kind, ())]
    if failed := next((row for row in rows if not row.reachable), None):
        said = failed.error or f"The last read of {registry_label(failed.kind)} failed"
        return Reading.unavailable(said.rstrip("."))
    if rows:
        labels = " and ".join(dict.fromkeys(registry_label(row.kind) for row in rows))
        return Reading.unavailable(held.rstrip(".") or f"The reading of {labels} holds nothing to judge it by")
    waited = " or ".join(dict.fromkeys(registry_label(kind) for kind in kinds)) or "it"
    return Reading.unavailable(f"Waiting on the first read of {waited}")


def _setup_url() -> str:
    return reverse("control_plane:connections") + "#connection-setup-heading"


def _sign_in(_subject) -> Reading:
    if sso_only():
        return Reading.met(detail="No password sign-in is installed.")
    return Reading.unmet("Password sign-in installed", detail="Password sign-in is installed.")


# Of HQ's standard, and measured on every request too (``request_standard``).
SIGN_IN = Check(
    "sign-in",
    "Sign-in is single sign-on only",
    _sign_in,
    "a guessed or reused password",
    "Turn password sign-in off in HQ's settings once single sign-on works.",
    unmet="Password sign-in is installed",
    holds="Every session starts with single sign-on.",
    boundary="Human identity",
    mechanism="HQ's sign-in settings",
)


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
        return Reading.unmet("An allow-all rule")
    return Reading.met(counted(len(policy.grants) + len(policy.acls), "rule", "rules"))


def _tailnet_lock(_inputs) -> Reading:
    lock = tailnet.policy().lock
    if not lock:
        # A read that failed keeps the last reading, so with none it says why.
        return _not_read((TAILNET_LOCK_KIND,))
    keys = len(lock.get("signers") or ())
    return Reading.met(counted(keys, "signing key", "signing keys")) if lock.get("enabled") else Reading.unmet("Off")


def _scope(inputs) -> Reading:
    """Unmet only where the provider takes a scoped token (its setup lists
    permissions); a login has no scoped form."""

    scoped = {setup.provider for setup in setups() if setup.permissions}
    connections = tuple(connection for group in inputs.groups for connection in group.connections)
    broad = [
        (group, connection)
        for group in inputs.groups
        for connection in group.connections
        if connection.instance.credential_model == "coarse" and connection.instance.kind in scoped
    ]
    if broad:
        return Reading.unmet(
            f"Full account access: {', '.join(sorted(connection.instance.label for _, connection in broad))}",
            read_capabilities=_gates_of(group for group, _ in broad),
        )
    if not any(connection.instance.credential_model != "none" for connection in connections):
        return Reading.unavailable("No connections yet", fix="add a connection", fix_url=_setup_url())
    if any(connection.instance.credential_model == UNKNOWN_CREDENTIAL for connection in connections):
        return Reading.unavailable("Unknown credential type")
    return _scoped_where_offered(connections, scoped)


def _gates_of(groups) -> tuple[str, ...]:
    """The union of the read gates of the connection families named."""

    return tuple(dict.fromkeys(name for group in groups for name in required_capability_names(group.spec)))


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
    return Reading.met(capfirst(evidence), limited=bool(whole))


def _approval(_inputs) -> Reading:
    found = repository(None)
    if found is None:
        return _not_read((REPOSITORY_KIND,))
    production = found.production_environment
    if production is None:
        return Reading.unmet("No production environment")
    reviewers = production.get("reviewers") or ()
    return Reading.met(f"Reviewers: {', '.join(reviewers)}") if reviewers else Reading.unmet("No reviewers")


# ``why`` is what each stops, read after "Stops" in its sentence.
STANDARD: tuple[Check, ...] = (
    Check(
        "edge",
        "The proxy lets in tailnet addresses only",
        _edge,
        "the proxy exposing HQ beyond the tailnet",
        "Give HQ's proxy host an access list that allows the two tailnet ranges and denies everything else.",
        unmet="The proxy lets in more than the tailnet",
        holds="The proxy refuses any address outside the tailnet.",
    ),
    Check(
        "host-firewall",
        "The firewall requires the tailnet interface",
        _firewall,
        "a forged tailnet address arriving another way",
        "Accept HQ's port only on the tailnet interface in the host firewall.",
        unmet="The firewall accepts HQ's port by address alone",
        holds="The firewall drops a tailnet address that arrives on any other interface.",
    ),
    Check(
        "tailnet-policy",
        "The tailnet policy has no allow-all rule",
        _tailnet_policy,
        "any device on the tailnet opening HQ",
        "Replace the allow-all rule with grants that name who reaches HQ.",
        unmet="The tailnet policy lets every device reach every device",
        holds="A device on the tailnet can open only what the policy allows it.",
    ),
    Check(
        "tailnet-lock",
        "Tailnet lock is on",
        _tailnet_lock,
        "a device added to your tailnet without your key",
        "Turn on tailnet lock, with signing keys on devices you hold.",
        unmet="Tailnet lock is off",
        holds="A device added to your tailnet without your key is refused by tailnet lock.",
    ),
    SIGN_IN,
    Check(
        "scope",
        "Each token is scoped where its service allows it",
        _scope,
        "a stolen token reaching the whole account",
        "Replace the full-account token with a scoped one holding the permissions its connection lists.",
        unmet="A full-account token is used where a scoped one is offered",
        holds="A stolen token reaches only what its scopes allow.",
    ),
    Check(
        "approval",
        "Deploys wait for your approval",
        _approval,
        "a merge reaching production before you see it",
        "Add yourself as a required reviewer on the repository's production environment.",
        unmet="Deploys do not wait for approval",
        holds="Nothing reaches production until you approve it.",
    ),
)


def posture(inputs: Inputs) -> Measurement:
    """HQ against its standard: under Advanced HQ Posture an unmet check is a
    fault; otherwise an open item, shown quietly."""

    return measure(inputs, STANDARD, enforced=advanced())


def estate_posture(groups) -> Measurement:
    """For callers that have not read the proxy's control for HQ's name."""

    _policy, edge = observed_connection_controls(next(iter(hq_hostnames()), ""))
    return posture(Inputs(edge, tuple(groups)))


def guide() -> str:
    return docs_url("Posture.md")


def as_part(found: Measurement) -> StandardPart:
    """HQ's standard as the connections read answers it."""

    return {
        "advanced": found.enforced,
        "met": found.met,
        "unread": found.unread,
        "checks": [
            {
                "id": result.check.id,
                "label": result.check.label,
                "met": None if result.state == Standing.UNAVAILABLE else result.met,
                "evidence": result.reading.evidence,
                "qualified": result.reading.limited,
                "stops": result.check.why,
                "remedy": result.reading.fix or None,
                "remedy_url": result.reading.fix_url or None,
            }
            for result in found.results
        ],
    }

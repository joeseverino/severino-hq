"""HQ's standard: the checks beyond what every HQ enforces (docs/Posture.md).

With ``SEVERINO_POSTURE=advanced`` each unmet check is also a finding.
"""

from collections import Counter
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

from .labels import dotted
from .standard_findings import Measured, Per, Standard
from .standards import Check, Measurement, Reading, Standing, measure
from .tone import Tone
from .ui import counted
lazy from . import cadence, tailnet
lazy from .connection_catalog import connection_catalog
lazy from .connection_security import estate_readings, ingress_kinds, tailnet_policy_reading
lazy from .connection_setup import docs_url, setups
lazy from .connections import connection_rows
lazy from .derivations import present
lazy from .facts import stored_snapshots
lazy from .github_estate import REPOSITORY_KIND
lazy from .hq_context.release_part import repository
lazy from .hq_self import hq_hostnames
lazy from .integration_validation import required_capability_names
lazy from .routes import reverse


def advanced() -> bool:
    return settings.SEVERINO_POSTURE == "advanced"


@dataclass(frozen=True, slots=True, kw_only=True)
class Inputs:
    """The proxy's and the host firewall's readings for HQ's name, and the connection families."""

    edge: Reading
    firewall: Reading
    groups: tuple[Any, ...]


def _observed(reading: Reading, kinds: tuple[str, ...]) -> Reading:
    """A provider's reading as judged, or where it could not be judged, why
    (``_not_read``), given what the reading held instead of an answer."""

    if reading.standing is Standing.UNAVAILABLE:
        return _not_read(kinds, held=reading.detail)
    return reading


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


# The subject of HQ's standard and of the request standard alike carries the
# proxy's and the firewall's readings, so each check below is stated once and
# reads them the same way on every page.
def _edge(subject) -> Reading:
    return _observed(subject.edge, ingress_kinds())


def _firewall(subject) -> Reading:
    return _observed(subject.firewall, (FIREWALL_KIND,))


def _tailnet_policy(_inputs) -> Reading:
    return _observed(tailnet_policy_reading(), (TAILNET_POLICY_KIND,))


def _tailnet_lock(_inputs) -> Reading:
    lock = tailnet.policy().lock
    if not lock:
        # A read that failed keeps the last reading, so with none it says why.
        return _not_read((TAILNET_LOCK_KIND,))
    keys = len(lock.get("signers") or ())
    return Reading.met(counted(keys, "signing key", "signing keys")) if lock.get("enabled") else Reading.unmet("Off")


def _scope(inputs) -> Reading:
    """One judgment of every credential, from what the checks of its abilities
    found (``connection_catalog``): a permission missing, or full account
    access where the service offers a scoped token (its setup lists
    permissions), is unmet; one HQ could not check is not read; a login whose
    service offers nothing narrower is met, by name, so it is never read as a
    clean pass."""

    scoped = {setup.provider for setup in setups() if setup.permissions}
    pairs = tuple((group, connection) for group in inputs.groups for connection in group.connections)
    evidence = Counter(state.evidence for _group, connection in pairs for state in connection.abilities)

    def having(*found: str) -> tuple:
        return tuple(pair for pair in pairs if any(state.evidence in found for state in pair[1].abilities))

    def named(found: tuple) -> str:
        return ", ".join(sorted({connection.instance.label for _group, connection in found}))

    missing = having("missing", "revoked")
    broad = tuple(pair for pair in having("coarse") if pair[1].instance.kind in scoped)
    whole = tuple(pair for pair in having("coarse") if pair[1].instance.kind not in scoped)
    if missing or broad:
        return Reading.unmet(
            dotted(
                f"Permission missing: {named(missing)}" if missing else "",
                f"Full account access: {named(broad)}" if broad else "",
            ),
            read_capabilities=_gates_of(group for group, _connection in (*missing, *broad)),
            detail="A credential lacks a permission HQ needs."
            if missing
            else "A credential has full account access where its service offers a scoped token.",
        )
    if not evidence:
        return Reading.unavailable(
            "No connection reads or changes anything yet", fix="add a connection", fix_url=_setup_url()
        )
    unchecked = evidence["undeclared"] + evidence["unverified"]
    unknown = evidence["unknown"]
    # Counted by connection, as every other line on the page counts.
    keyless = sum(
        all(state.evidence == "not_applicable" for state in connection.abilities)
        for _group, connection in pairs
        if connection.abilities
    )
    said = capfirst(
        dotted(
            f"{evidence['verified']} checked" if evidence["verified"] else "",
            f"{keyless} need no key" if keyless else "",
            f"nothing narrower offered for {named(whole)}" if whole else "",
            f"{unchecked} not checked" if unchecked else "",
            f"{unknown} not reported" if unknown else "",
        )
    )
    if unchecked or unknown:
        return Reading.unavailable(said, detail="HQ cannot confirm these credentials are limited to what it needs.")
    if whole:
        return Reading.met(said, limited=True, detail="These services offer nothing narrower than full account access.")
    return Reading.met(said, detail="Each service confirmed the permissions HQ needs.")


def _gates_of(groups) -> tuple[str, ...]:
    """The union of the read gates of the connection families named."""

    return tuple(dict.fromkeys(name for group in groups for name in required_capability_names(group.spec)))


def _approval(_inputs) -> Reading:
    found = repository(None)
    if found is None:
        return _not_read((REPOSITORY_KIND,))
    production = found.production_environment
    if production is None:
        return Reading.unmet("No production environment")
    reviewers = production.get("reviewers") or ()
    return Reading.met(f"Reviewers: {', '.join(reviewers)}") if reviewers else Reading.unmet("No reviewers")


# Of HQ's standard, and drawn on a request's path too (``request_standard``),
# where only a reading of it exists.
EDGE = Check(
    "edge",
    "The proxy lets in tailnet addresses only",
    _edge,
    "the proxy exposing HQ beyond the tailnet",
    "Give HQ's proxy host an access list that allows the two tailnet ranges and denies everything else.",
    applies=lambda subject: subject.edge is not None,
    unmet="The proxy lets in more than the tailnet",
    holds="The proxy refuses any address outside the tailnet.",
    hop="ingress",
    boundary="Reverse proxy edge",
    mechanism="The proxy's access list",
)
FIREWALL = Check(
    "host-firewall",
    "The firewall requires the tailnet interface",
    _firewall,
    "a forged tailnet address arriving another way",
    "Accept HQ's port only on the tailnet interface in the host firewall.",
    applies=lambda subject: subject.firewall is not None,
    unmet="The firewall accepts HQ's port by address alone",
    holds="The firewall drops a tailnet address that arrives on any other interface.",
    hop="machine",
    boundary="Network",
    mechanism="The host firewall's rule for HQ's port",
)
# Of HQ's standard, and of the connections standard (``connection_security``).
SCOPE = Check(
    "scope",
    "Tokens hold the permissions HQ uses, and no more",
    _scope,
    "a stolen token reaching the whole account",
    "Give the connection a scoped token holding the permissions it lists.",
    unmet="A token lacks a permission or reaches further than it must",
    holds="Each token holds what HQ uses, so a stolen one reaches only that.",
    boundary="Credentials",
    mechanism="Each connection's permission check",
)

# ``why`` is what each stops, read after "Stops" in its sentence.
STANDARD: tuple[Check, ...] = (
    EDGE,
    FIREWALL,
    Check(
        "tailnet-policy",
        "The tailnet policy has no allow-all rule",
        _tailnet_policy,
        "any device on the tailnet opening HQ",
        "Replace the allow-all rule with grants that name who reaches HQ.",
        unmet="The tailnet policy lets every device reach every device",
        holds="A device on the tailnet can open only what the policy allows it.",
        boundary="Zero trust policy",
        mechanism="The tailnet policy",
    ),
    Check(
        "tailnet-lock",
        "Tailnet lock is on",
        _tailnet_lock,
        "a device added to your tailnet without your key",
        "Turn on tailnet lock, with signing keys on devices you hold.",
        unmet="Tailnet lock is off",
        holds="A device added to your tailnet without your key is refused by tailnet lock.",
        boundary="Device identity",
        mechanism="Tailnet lock, read by the host's own daemon",
    ),
    SIGN_IN,
    SCOPE,
    Check(
        "approval",
        "Deploys wait for your approval",
        _approval,
        "a merge reaching production before you see it",
        "Add yourself as a required reviewer on the repository's production environment.",
        unmet="Deploys do not wait for approval",
        holds="Nothing reaches production until you approve it.",
        boundary="Release",
        mechanism="The production environment's reviewers, read by the GitHub App",
    ),
)


def posture(inputs: Inputs) -> Measurement:
    """HQ against its standard: under Advanced HQ Posture an unmet check is a
    fault; otherwise an open item, shown quietly."""

    return measure(inputs, STANDARD, enforced=advanced())


def estate_inputs(groups) -> Inputs:
    """HQ's standard's subject, read from the estate's shared readings."""

    edge, firewall = estate_readings(next(iter(hq_hostnames()), ""))
    return Inputs(edge=edge, firewall=firewall, groups=tuple(groups))


def estate_posture(groups) -> Measurement:
    """For callers that have not read the proxy's and firewall's readings for HQ."""

    return posture(estate_inputs(groups))


def guide() -> str:
    return docs_url("Posture.md")


def _hq(estate) -> tuple[Measured, ...]:
    """HQ, under Advanced HQ Posture, measured from the connections its reader may read."""

    if estate.principal is None or not advanced():
        return ()
    return (Measured(measurement=estate_posture(connection_catalog(principal=estate.principal))),)


POSTURE_STANDARD = Standard(
    name="posture-unmet",
    title="A check of HQ's standard is not met",
    severity=Tone.ATTENTION,
    checks=STANDARD,
    subjects=_hq,
    raises=Per.CHECK,
    operator_action="Meet the check, or turn Advanced HQ Posture off.",
    no_help_reason="HQ does not change these settings.",
    bars={Tone.ATTENTION: "a check of the standard HQ holds itself to is not met"},
)
RULES = (POSTURE_STANDARD.rule(read_through="connections"),)

"""Findings about a connection's credential: what it lacks and when it lapses.

Read off the facts ``topology`` puts on each connection node from
``credential_mint.credential_fixes``. Each carries the one approved fix: an
operator command that mints a replacement on the operator's machine. HQ offers
it and never runs it. Rules return a ``Finding``'s fields; ``findings`` builds
the finding, so this module does not import it.
"""

from typing import Any

from hq.domains.control_plane.provider_adapters.contracts import (
    ADDRESS_FAILURE,
    NETWORK_FAILURE,
    REFUSALS,
)

from .derivations import reached
from .finding_model import THEN_READ_NOW, FindingRule, OperatorStep, built_findings, fact_values
from .timestamps import moment
from .ui import counted

# Fact keys a connection node carries; ``topology`` writes them.
FAILURE = "connection-failure"
ENDPOINT = "connection-endpoint"
MISSING = "credential-missing"
UNSEEN = "credential-unseen"
EXPIRES = "credential-expires"
MINT = "credential-mint"
MINT_NOTE = "credential-mint-note"
BY_HAND = "credential-by-hand"


def credential_facts(fix: Any) -> tuple[tuple[str, str], ...]:
    """The facts one ``CredentialFix`` puts on its connection node."""

    if fix is None or not fix.needed:
        return ()
    found = [(MISSING, name) for name in fix.missing]
    found += [(UNSEEN, label) for label in fix.unseen]
    if fix.expiring:
        found.append((EXPIRES, fix.expires_at.isoformat()))
    if fix.command:
        found.append((MINT, fix.command))
    found += [(MINT_NOTE, gap) for gap in fix.gaps]
    if fix.by_hand:
        found.append((BY_HAND, fix.by_hand))
    return tuple(found)


def operator_steps(
    command: str, notes: tuple[str, ...], by_hand: str, missing: tuple[str, ...]
) -> tuple[OperatorStep, ...]:
    """The mint step for one credential, or none when there is nothing to offer."""

    if not command and not notes and not by_hand:
        return ()
    if by_hand:
        notes += (
            f"Or by hand: {by_hand}"
            + (f", adding {', '.join(missing)}." if missing else "."),
        )
    return (
        OperatorStep(
            label="Make a new token on your Mac" if command else "Make a new token",
            command=command,
            notes=(*notes, THEN_READ_NOW),
        ),
    )


def fix_steps(fix: Any) -> tuple[OperatorStep, ...]:
    """The mint step a ``CredentialFix`` offers, for a page that holds the fix."""

    if fix is None or not fix.needed:
        return ()
    return operator_steps(fix.command, fix.gaps, fix.by_hand, fix.missing)


def mint_steps(node: Any) -> tuple[OperatorStep, ...]:
    """The mint step a connection node's facts describe, for a finding."""

    return operator_steps(
        next(iter(fact_values(node, MINT)), ""),
        fact_values(node, MINT_NOTE),
        next(iter(fact_values(node, BY_HAND)), ""),
        fact_values(node, MISSING),
    )


# Why a connection's probe failed, as a finding's evidence says it.
FAILURE_LABELS = {
    "credential": "Credential refused",
    "permission": "Permission refused",
    ADDRESS_FAILURE: "The address is not the API",
    NETWORK_FAILURE: "No answer",
}


def failure_of_node(node: Any) -> str:
    """Why the connection's last probe failed, one of ``FAILURES``, or ""."""

    return next(iter(fact_values(node, FAILURE)), "")


def answer_steps(node: Any) -> tuple[OperatorStep, ...]:
    """The fix for a connection that does not answer, following why it did not."""

    from .credential_mint import address_fields

    failure = failure_of_node(node)
    endpoint = next(iter(fact_values(node, ENDPOINT)), "")
    if failure == ADDRESS_FAILURE:
        return (
            OperatorStep(
                label=f"Point the connection at {_service(node)}'s own API address",
                notes=(
                    *((f"{endpoint} is not the API.",) if endpoint else ()),
                    "Set the address in the connection's 1Password item, "
                    f"{address_fields()}.",
                    THEN_READ_NOW,
                ),
            ),
        )
    if failure == NETWORK_FAILURE:
        where = _host(endpoint) or "the machine it points at"
        return (
            OperatorStep(
                label=f"Check that {where} is up and the controller can reach it",
                notes=(THEN_READ_NOW,),
            ),
        )
    if failure in REFUSALS:
        return mint_steps(node) or (
            OperatorStep(
                label="Replace the credential in the connection's 1Password item",
                notes=(THEN_READ_NOW,),
            ),
        )
    return ()


def _service(node: Any) -> str:
    """What a connection talks to, by the name its owner knows it by."""

    return node.subtitle or "the service"


def _host(endpoint: str) -> str:
    from urllib.parse import urlsplit

    text = str(endpoint or "").strip()
    if "://" in text:
        return urlsplit(text).hostname or ""
    return text.rsplit(":", 1)[0] if text.count(":") == 1 else text


def missing_permissions(estate: Any) -> tuple[dict[str, Any], ...]:
    """A valid credential the provider refuses some readings, named by permission."""

    found = []
    for node in estate.nodes():
        if node.kind != "connection":
            continue
        missing = fact_values(node, MISSING)
        if not missing:
            continue
        unseen = fact_values(node, UNSEEN)
        found.append(
            {
                "rule": "credential-missing-permissions",
                "subject": node.id,
                "title": f"{node.label}'s token is missing {counted(len(missing), 'permission')}",
                "severity": "attention",
                "explanation": (
                    f"The token works, but {_service(node)} will not let it read "
                    f"{', '.join(unseen) or 'everything HQ asks for'}. "
                    "Make a new token that includes them."
                ),
                "evidence": (
                    *(("Missing", name) for name in missing),
                    *(("Cannot read", label) for label in unseen),
                ),
                "steps": mint_steps(node),
            }
        )
    return tuple(sorted(found, key=lambda finding: finding["title"]))


def expiring(estate: Any) -> tuple[dict[str, Any], ...]:
    """A credential inside its renewal window, or past it."""

    from hq.domains.control_plane.provider_spec import expiry_phrase

    from .expiry import days_until
    from .moments import span

    found = []
    for node in estate.nodes():
        if node.kind != "connection":
            continue
        stamp = next(iter(fact_values(node, EXPIRES)), "")
        when = moment(stamp)
        if when is None:
            continue
        expired = reached(when)
        left = days_until(when)
        found.append(
            {
                "rule": "credential-expiring",
                "subject": node.id,
                "title": (
                    f"{node.label}'s token has expired"
                    if expired
                    else f"{node.label}'s token expires in {span(left)}"
                ),
                "severity": "serious" if expired else "attention",
                "explanation": (
                    (
                        f"HQ cannot read anything through {node.label} until it is replaced."
                        if expired
                        else f"After that HQ cannot read anything through {node.label}."
                    )
                    + " Make a new one now."
                ),
                "evidence": (("Expires", expiry_phrase(stamp)),),
                "steps": mint_steps(node),
            }
        )
    return tuple(sorted(found, key=lambda finding: finding["title"]))


_CANNOT_ISSUE = "HQ cannot issue a token."

# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        "credential-missing-permissions",
        "A token is missing permissions",
        "attention",
        lambda estate: built_findings(missing_permissions(estate)),
        operator_action=(
            f"Make a new token with the command on the connection's page. {THEN_READ_NOW}"
        ),
        no_help_reason=_CANNOT_ISSUE,
    ),
    FindingRule(
        "credential-expiring",
        "A token is expiring",
        "attention",
        lambda estate: built_findings(expiring(estate)),
        operator_action=(
            f"Make a new token with the command on the connection's page before it expires. {THEN_READ_NOW}"
        ),
        no_help_reason=_CANNOT_ISSUE,
    ),
)

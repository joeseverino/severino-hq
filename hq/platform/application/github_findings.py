"""Findings from what the GitHub App's hourly allowance says: who else spends it.

GitHub records nothing a person can read when a leaked App key or token is
used for reads, so what everything else spent of the installation's hour,
beside HQ's own sweeps, is the one sign there is (docs/Security.md). The
allowance reading carries it (``observations.github``), and the rule is
gathered: it reads each GitHub connection's allowance records, and speaks
once their spend passes ``OTHERS_SPENT_ATTENTION``.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from hq.domains.control_plane.observations.github import ALLOWANCE_KIND, OTHERS_SPENT_ATTENTION, PROVIDER

from .facts import records_for_connection
from .finding_model import Finding, FindingEstate, FindingRule
from .finding_steps import Do, Run
from .moments import moment, when
from .tone import Tone

RULE = "github-others-spent"

# GitHub's own command for a private key's fingerprint, from "Managing private
# keys for GitHub Apps": what the App's settings list beside each key.
FINGERPRINT_COMMAND = "openssl rsa -in PATH_TO_PEM_FILE -pubout -outform DER | openssl sha256 -binary | openssl base64"

STEPS = (
    Do(
        "Open the App's settings on GitHub and count the keys under Private keys. Each one listed can sign for the App."
    ),
    Run(
        label="Take the fingerprint of the key HQ holds, and compare it with the ones GitHub lists.",
        command=FINGERPRINT_COMMAND,
        notes=(
            "The GitHub App connection in HQ shows the same fingerprint beside the App's name.",
            "GitHub lists no key through its API, so only its settings page can say how many there are.",
        ),
    ),
    Do(
        "Delete every key that is not HQ's. Then rotate HQ's: generate a new key, store it with "
        "mise run wire-github-app, and delete the old one.",
        notes=("A token issued under the old key lasts up to an hour after the key is deleted.",),
    ),
)


def _between(since: str, until: str) -> str:
    start, end = moment(since), moment(until)
    if start is None or end is None:
        return "this hour"
    return f"between {when(start)} and {when(end)}"


@dataclass(frozen=True, slots=True)
class OthersSpent:
    """What everything besides HQ's sweeps spent of one installation's hour."""

    account: str
    requests: int
    since: str
    until: str


def _others_spent(record: Mapping[str, Any]) -> OthersSpent | None:
    """What an allowance record says others spent, once it passes ``OTHERS_SPENT_ATTENTION``."""

    spent = record.get("others_spent")
    if spent is None or int(spent) < OTHERS_SPENT_ATTENTION:
        return None
    return OthersSpent(
        str(record.get("account", "")), int(spent), str(record.get("others_since", "")), str(record["started_at"])
    )


def others_spent(estate: FindingEstate) -> tuple[Finding, ...]:
    """A GitHub App connection whose installation something besides HQ's
    sweeps spent more of in one hour than the deploy pipeline and the
    dependency update run take."""

    found = []
    for node in estate.nodes():
        if node.kind != "connection" or node.provider != PROVIDER:
            continue
        said = (_others_spent(record) for record in records_for_connection(ALLOWANCE_KIND, node.connection_ref))
        for spend in dict.fromkeys(item for item in said if item is not None):
            account = spend.account
            between = _between(spend.since, spend.until)
            spent = f"{spend.requests:,}"
            on = f" on {account}" if account else ""
            found.append(
                Finding(
                    rule=RULE,
                    subject=node.id,
                    part=account,
                    title=f"Something besides HQ spent {spent} GitHub requests{on}",
                    severity=Tone.ATTENTION,
                    explanation=(
                        f"The App's installation{on} answered {spent} requests {between} that HQ did not "
                        "make. Its key or a token may have leaked, or another program shares the App."
                    ),
                    evidence=(
                        ("Requests", spent),
                        ("When", between),
                        *((("Account", account),) if account else ()),
                        ("Usual at most", f"{OTHERS_SPENT_ATTENTION:,} an hour"),
                    ),
                    steps=STEPS,
                )
            )
    return tuple(sorted(found, key=lambda finding: finding.title))


RULES: tuple[FindingRule, ...] = (
    FindingRule(
        RULE,
        "Something besides HQ spends its App's hourly allowance",
        Tone.ATTENTION,
        others_spent,
        gathered=True,
        operator_action=("Delete every App key on GitHub that is not HQ's, then rotate HQ's."),
        no_help_reason="HQ cannot see or delete the App's keys: GitHub lists them only on the App's settings page.",
        bars={Tone.ATTENTION: "something besides HQ spends the App's requests in an hour"},
    ),
)

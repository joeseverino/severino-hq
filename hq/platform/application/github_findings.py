"""Findings from what the GitHub App's hourly allowance says: who else spends it.

GitHub records nothing a person can read when a leaked App key or token is
used for reads, so what everything else spent of the installation's hour,
beside HQ's own sweeps, is the one sign there is (docs/Security.md). The
allowance reading carries it, and puts it on the connection's node once it
passes ``OTHERS_SPENT_ATTENTION`` (``observations.github``).
"""

from hq.domains.control_plane.observations.github import OTHERS_SPENT, OTHERS_SPENT_ATTENTION

from .finding_model import Finding, FindingEstate, FindingRule, OperatorStep, connections_carrying
from .moments import when
from .timestamps import moment

RULE = "github-others-spent"

# GitHub's own command for a private key's fingerprint, from "Managing private
# keys for GitHub Apps": what the App's settings list beside each key.
FINGERPRINT_COMMAND = "openssl rsa -in PATH_TO_PEM_FILE -pubout -outform DER | openssl sha256 -binary | openssl base64"

STEPS = (
    OperatorStep(
        label=(
            "Open the App's settings on GitHub and count the keys under Private keys: "
            "each one listed can sign for the App."
        ),
    ),
    OperatorStep(
        label="Take the fingerprint of the key HQ holds, and compare it with the ones GitHub lists.",
        command=FINGERPRINT_COMMAND,
        notes=(
            "The GitHub App connection in HQ shows the same fingerprint beside the App's name.",
            "GitHub lists no key through its API, so only its settings page can say how many there are.",
        ),
    ),
    OperatorStep(
        label=(
            "Delete every key that is not HQ's. Then generate a new key, store it with mise run wire-github-app, "
            "and delete the old one, so a token or key that leaked stops working."
        ),
        notes=("A token issued under the old key lasts up to an hour after the key is deleted.",),
    ),
)


def _between(since: str, until: str) -> str:
    start, end = moment(since), moment(until)
    if start is None or end is None:
        return "this hour"
    return f"between {when(start)} and {when(end)}"


def others_spent(estate: FindingEstate) -> tuple[Finding, ...]:
    """A GitHub App connection whose installation something besides HQ's
    sweeps spent more of in one hour than the deploy pipeline and the
    dependency update run take."""

    found = []
    for node, values in connections_carrying(estate, OTHERS_SPENT):
        for value in values:
            account, requests, since, until = [*value.split("|"), "", "", ""][:4]
            between = _between(since, until)
            spent = f"{int(requests):,}"
            on = f" on {account}" if account else ""
            found.append(
                Finding(
                    rule=RULE,
                    subject=node.id,
                    scope=account,
                    title=f"Something besides HQ spent {spent} GitHub requests{on}",
                    severity="attention",
                    explanation=(
                        f"The App's installation{on} answered {spent} requests {between} that HQ did not "
                        "make, more than the deploy pipeline and the dependency update run take in an hour. It can "
                        "mean the App's private key or a token issued under it leaked, or another program shares the "
                        "App, such as a development copy of HQ or a script. GitHub keeps no record of reads, so this "
                        "count is the only sign there is."
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
        "attention",
        others_spent,
        operator_action=(
            "Compare the App's private keys on GitHub with the one HQ holds, delete any that is not HQ's, "
            "and rotate HQ's."
        ),
        no_help_reason="HQ cannot see or delete the App's keys: GitHub lists them only on the App's settings page.",
    ),
)

"""Findings about the GitHub App itself: who else spends its hourly
allowance, and whether its webhook reaches HQ.

GitHub records nothing readable when a leaked App key is used for reads, so
what others spent of the hour is the one sign there is (docs/Security.md).
HQ is never dialed, so GitHub's delivery log is the one place a delivery the
events Worker refused, or never received, shows.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from django.utils.text import get_text_list

from hq.domains.control_plane.observations.github import (
    ALLOWANCE_KIND,
    OTHERS_SPENT_ATTENTION,
    PROVIDER,
    WEBHOOK_KIND,
    Delivery,
    app_settings,
)
from hq.domains.control_plane.provider_adapters import INBOUND

from .entity_links import web_url
from .facts import records_for_connection
from .finding_model import Finding, FindingEstate, FindingRule, opens
from .finding_steps import Do, Run
from .moments import moment, when, when_at
from .tone import Tone
from .ui import counted

RULE = "github-others-spent"
WEBHOOK_RULE = "github-webhook"

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
    return f"between {when_at(start)} and {when_at(end)}"


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
                    explanation="Check whether its key or a token leaked.",
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


# Each event HQ takes from the App, by the name the App's settings tick it under.
_TAKEN = tuple(dict.fromkeys(event.partition(".")[0] for event in INBOUND[PROVIDER].events))


def _unsubscribed(record: Mapping[str, Any]) -> tuple[str, ...]:
    """The events HQ takes that the App is not subscribed to, once its
    webhook is in use: one that has delivered, or subscribes to any of them.
    An App never pointed at HQ is not set up, which is nothing to raise."""

    subscribed = set(record.get("events") or ())
    if not record.get("deliveries") and not subscribed & set(_TAKEN):
        return ()
    return tuple(event for event in _TAKEN if event not in subscribed)


def _webhook(record: Mapping[str, Any], subject: str, now) -> Finding | None:
    failing = int(record.get("failing") or 0)
    missing = _unsubscribed(record)
    if not failing and not missing:
        return None
    settings = web_url(app_settings(record))
    events = get_text_list(list(missing), "and")
    facts: list[tuple[str, str]] = []
    if failing:
        newest = Delivery.model_validate(record.get("newest") or {"event": ""})
        title = f"{counted(failing, 'webhook delivery', 'webhook deliveries')} to HQ failed"
        facts += [
            ("Answer", f"{newest.status_code or ''} {newest.status}".strip() or "None"),
            ("Event", newest.event),
            ("Delivered", when(newest.delivered_at, now=now)),
        ]
    else:
        title = f"The GitHub App does not send {events}"
    if missing:
        facts.append(("Not subscribed", events))
    return Finding(
        rule=WEBHOOK_RULE,
        subject=subject,
        part=str(record.get("app", "")),
        title=title,
        severity=Tone.ATTENTION,
        explanation="Changes on GitHub show only once HQ next reads them.",
        evidence=tuple(facts),
        primary=opens("Open the App's settings", settings) if settings else None,
        steps=(
            *((Do(f"Tick {events} under Subscribe to events, then save."),) if missing else ()),
            *((Do("Read the failed delivery's answer under Advanced, in Recent Deliveries."),) if failing else ()),
        ),
    )


def webhook(estate: FindingEstate) -> tuple[Finding, ...]:
    """A GitHub App whose newest webhook deliveries failed, or that is not
    subscribed to an event HQ takes from it (``INBOUND``)."""

    found = (
        _webhook(record, node.id, estate.now)
        for node in estate.nodes()
        if node.kind == "connection" and node.provider == PROVIDER
        for record in records_for_connection(WEBHOOK_KIND, node.connection_ref)
    )
    return tuple(finding for finding in found if finding is not None)


RULES: tuple[FindingRule, ...] = (
    FindingRule(
        RULE,
        "App requests HQ did not make",
        Tone.ATTENTION,
        others_spent,
        gathered=True,
        operator_action="Delete every App key on GitHub that is not HQ's, then rotate HQ's.",
        no_help_reason="GitHub lists the App's keys only on its settings page.",
        bars={Tone.ATTENTION: "something besides HQ spends the App's requests in an hour"},
    ),
    FindingRule(
        WEBHOOK_RULE,
        "App webhook not reaching HQ",
        Tone.ATTENTION,
        webhook,
        gathered=True,
        operator_action="Fix what the failed delivery's answer names, and tick each event HQ takes, in the App's settings.",
        no_help_reason="HQ reads the App's settings and never changes them.",
        bars={Tone.ATTENTION: "the App's newest webhook deliveries failed, or it is not sent an event HQ takes"},
        clears="a delivery succeeds and the App is subscribed to every event HQ takes",
    ),
)

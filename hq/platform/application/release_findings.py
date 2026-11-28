"""Findings about the release of HQ that production runs: what to check once
it went live, where no other rule says it.

A unit that failed, a deploy that failed verification, one on its way or
waiting on a reviewer each have their own rule (``unit_findings``,
``repository_findings``, ``delivery_findings``); these read what only the
release says: how its deploy ended, and the pages that failed since.
"""

from collections.abc import Mapping
from typing import Any

from django.db.models import Count, Min

from hq.platform.core.models import ServerError

from .finding_model import Finding, FindingEstate, FindingRule, opens
from .moments import moment, when
from .routes import reverse
from .tone import Tone
from .ui import counted, ended
lazy from . import deploy_outcomes, releases

CHECK_FAILED = "release-check-failed"
SERVER_ERRORS = "release-server-errors"
# How many failed pages a finding names, each with its count, before it counts the rest.
MOST_NAMED = 4


def _short(release: Mapping[str, Any]) -> str:
    return str(release.get("sha") or "")[:7]


def _page(release: Mapping[str, Any]) -> str:
    return reverse("releases:detail", args=[str(release.get("sha") or "")])


def _check_failed(estate: FindingEstate) -> tuple[Finding, ...]:
    release = releases.live()
    if release is None or not release.get("check_failed"):
        return ()
    declared = deploy_outcomes.outcome(release) or {}
    return (
        Finding(
            rule=CHECK_FAILED,
            subject="",
            part=str(release.get("sha") or ""),
            title=f"Release {_short(release)} went live with a check failed",
            severity=Tone.ATTENTION,
            explanation=ended(declared.get("description") or "A check its deploy ran failed"),
            evidence=(("Went live", when(release.get("ended_at"), now=estate.now)),),
            since=moment(release.get("ended_at")),
            primary=opens("Open release", _page(release)),
        ),
    )


def _server_errors(_estate: FindingEstate) -> tuple[Finding, ...]:
    release = releases.live()
    went_live = moment(release.get("ended_at")) if release is not None else None
    if release is None or went_live is None:
        return ()
    pages = list(
        ServerError.objects.filter(occurred_at__gt=went_live)
        .values("path")
        .annotate(errors=Count("pk"), first=Min("occurred_at"))
        .order_by("-errors", "path")
    )
    if not pages:
        return ()
    total = sum(page["errors"] for page in pages)
    named = pages[:MOST_NAMED]
    others = len(pages) - len(named)
    return (
        Finding(
            rule=SERVER_ERRORS,
            subject="",
            part=str(release.get("sha") or ""),
            title=f"{counted(total, 'server error', 'server errors')} since release {_short(release)} went live",
            severity=Tone.ATTENTION,
            explanation=ended(f"{counted(len(pages), 'page')} answered with an unhandled error since it went live"),
            evidence=(
                *((page["path"] or "HQ", counted(page["errors"], "error")) for page in named),
                *((("Other pages", counted(others, "page")),) if others else ()),
            ),
            since=min(page["first"] for page in pages),
            primary=opens("Open errors", reverse("errors:list")),
            offers=(opens("Open release", _page(release)),),
            count=total,
        ),
    )


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        CHECK_FAILED,
        "Releases live with a check failed",
        Tone.ATTENTION,
        _check_failed,
        operator_action="Read the failed check in the Deploy run's log on GitHub, then fix what it names.",
        no_help_reason="HQ reads that a check failed, not which.",
        bars={Tone.ATTENTION: "the release production runs went live with a check failed"},
        clears="a release goes live with every check passing",
        domain="hq.self",
        gathered=True,
    ),
    FindingRule(
        SERVER_ERRORS,
        "Server errors since a release",
        Tone.ATTENTION,
        _server_errors,
        operator_action="Read each error's traceback in Errors, then fix what raised it.",
        no_help_reason="HQ keeps what was raised, not why.",
        bars={Tone.ATTENTION: "a page answered with a server error since the release production runs went live"},
        clears="a release goes live and no page has answered with a server error since",
        domain="hq.errors",
        gathered=True,
    ),
)

"""Findings about the release of HQ that production runs: what to check once
it went live, where no other rule says it.

A unit that failed, a deploy that failed verification, one on its way or
waiting on a reviewer each have their own rule (``unit_findings``,
``repository_findings``, ``delivery_findings``); these read what only the
release says: how its deploy ended, the pages that failed since, and the
pages its rehearsal on production's copy drew heavier than the release
before's.
"""

from collections.abc import Mapping
from typing import Any

from django.db.models import Count, Min
from django.utils.text import get_text_list

from hq.platform.core.models import ServerError

from .finding_model import Finding, FindingEstate, FindingRule, opens
from .moments import moment, when
from .routes import reverse
from .thresholds import Threshold
from .tone import Tone
from .ui import counted, ended
lazy from . import deploy_outcomes, release_pages, releases

CHECK_FAILED = "release-check-failed"
SERVER_ERRORS = "release-server-errors"
HEAVIER = "release-pages-heavier"
# How many failed pages a finding names, each with its count, before it counts the rest.
MOST_NAMED = 4
# How far past the release before's a page's bytes, once or per row, grow
# before it is heavier. Its query counts are exact, so one more is heavier.
GROWTH = Threshold(0.10, "share")
# The measures compared by bytes; every other is a count of queries.
BY_BYTES = frozenset(("page_bytes", "bytes_per_row"))


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


def _grew(key: str, now: float | None, before: float | None) -> bool:
    if now is None or before is None:
        return False
    return now > before * (1 + GROWTH.value) if key in BY_BYTES else now > before


def _heavier(page: Mapping[str, Any]) -> str:
    """Each measure the page grew by past its bar, with the release before's
    figure; empty where none did or the release before drew no such page."""

    before = page["previous"]
    if before is None:
        return ""
    grew = [
        f"{heading} {written(page[key])} from {written(before[key])}"
        for heading, key, written in release_pages.COMPARED
        if _grew(key, page[key], before[key])
    ]
    return get_text_list(grew, "and")


def _pages_heavier(_estate: FindingEstate) -> tuple[Finding, ...]:
    release = releases.live()
    if release is None:
        return ()
    commit = str(release.get("sha") or "")
    heavier = [(page["page"], grew) for page in releases.get_release(commit)["pages"] if (grew := _heavier(page))]
    if not heavier:
        return ()
    named = heavier[:MOST_NAMED]
    others = len(heavier) - len(named)
    return (
        Finding(
            rule=HEAVIER,
            subject="",
            part=commit,
            title=f"{counted(len(heavier), 'page')} heavier in release {_short(release)} than the release before",
            severity=Tone.ATTENTION,
            explanation=ended("Its rehearsal on production's copy drew them with more queries or bytes"),
            evidence=(*named, *((("Other pages", counted(others, "page")),) if others else ())),
            since=moment(release.get("ended_at")),
            primary=opens("Open release", _page(release)),
            count=len(heavier),
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
    FindingRule(
        HEAVIER,
        "Pages heavier than the release before",
        Tone.ATTENTION,
        _pages_heavier,
        operator_action="Open the release's pages, then cut what each heavier page's new queries or bytes draw.",
        no_help_reason="HQ measures what a page cost, not which change made it cost more.",
        bars={
            Tone.ATTENTION: (
                "a page of the release production runs makes more queries in its rehearsal than in the release "
                f"before's, or draws more than {GROWTH} more bytes, once or per row"
            )
        },
        clears=(
            f"no page of the release production runs makes more queries or draws more than {GROWTH} more bytes "
            "than in the release before's rehearsal, or the release before had no page measured"
        ),
        domain="hq.self",
        gathered=True,
    ),
)

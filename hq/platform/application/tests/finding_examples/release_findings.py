from datetime import timedelta

from django.utils import timezone

from hq.domains.control_plane.observations.host import RehearsedPage
from hq.platform.core.models import ServerError

from ...release_snapshots import ReleaseReadings, keep
from . import Ago, Example, Examples, Raised, ago

KIND = "github.repository"
SOURCE = "https://github.com/example/hq"
LIVE = "a" * 40
EARLIER = "b" * 40


def deployment(sha: str, outcome: str, ended: Ago, *, state: str = "success", check_failed: bool = False) -> dict:
    """A deployment to production as the GitHub reading records one."""

    return {
        "environment": "production",
        "sha": sha,
        "created_at": ago(hours=ended.age.total_seconds() / 3600 + 0.1),
        "state": state,
        "url": f"{SOURCE}/actions/runs/9",
        "verified": [],
        "check_failed": check_failed,
        "outcome": outcome,
        "ended_at": ended,
        "image": f"ghcr.io/example/hq/composition@sha256:{sha[:64]}",
    }


def failed(*pages: tuple[str, timedelta]) -> None:
    """A request HQ failed on each page, that long ago."""

    for index, (path, age) in enumerate(pages):
        ServerError.objects.create(
            request_id=f"example-{index}",
            path=path,
            exception="builtins.RuntimeError",
            traceback="",
            occurred_at=timezone.now() - age,
        )


def drawn(
    page: str, *, queries: int = 20, repeated: int = 4, rows: int = 10, row_bytes: int = 8_000, html: int = 40_000
):
    """A page as a release's rehearsal drew it."""

    return {
        "page": page,
        "status": 200,
        "queries": queries,
        "repeated_queries": repeated,
        "rows": rows,
        "html_bytes": html,
        "row_html_bytes": row_bytes,
    }


def rehearsed(kept: dict[str, tuple[dict, ...]]) -> None:
    """Each release's snapshot, keeping the pages its rehearsal drew."""

    for commit, pages in kept.items():
        keep(commit, ReleaseReadings(pages=tuple(RehearsedPage.model_validate(page) for page in pages)))


def hq(
    *deployments: dict,
    errors: tuple[tuple[str, timedelta], ...] = (),
    kept: dict[str, tuple[dict, ...]] | None = None,
    raises=(),
) -> Example:
    """HQ built from example/hq, production's deployments newest first, the
    requests it failed and the pages each release's rehearsal drew."""

    record = {
        "connection_ref": "github",
        "repository": "example/hq",
        "url": SOURCE,
        "default_branch": "main",
        "head": {"sha": LIVE},
        "deployments": list(deployments),
    }
    return Example(
        readings={KIND: (record,)},
        given=lambda: (failed(*errors), rehearsed(kept or {})),
        settings={"SEVERINO_HQ_SOURCE": SOURCE},
        raises=raises,
    )


CLEAN = deployment(LIVE, "live", ago(hours=2))
CHECK_FAILED = deployment(LIVE, "live-check-failed", ago(hours=2), check_failed=True)
# A deploy that never went live leaves production on the release before it.
NOT_DEPLOYED = deployment(LIVE, "not-deployed", ago(hours=1), state="failure", check_failed=True)
EARLIER_CLEAN = deployment(EARLIER, "live", ago(days=2), state="inactive")
EARLIER_CHECK_FAILED = deployment(EARLIER, "live-check-failed", ago(days=2), state="inactive", check_failed=True)

# The pages the release before drew, and the release production runs drawing
# them within every bar: its page's bytes a little more, its rows more of the
# same size, and a page the release before did not have.
BEFORE = (drawn("/search/"), drawn("/infrastructure/topology/"), drawn("/domains/example.com/"))
WITHIN = (
    drawn("/search/", html=41_500),
    drawn("/infrastructure/topology/", rows=20, row_bytes=16_000, html=48_000),
    drawn("/domains/example.com/"),
    drawn("/connection/", queries=90, repeated=60),
)

EXAMPLES = (
    Examples(
        "release-check-failed",
        fires=(
            hq(
                CHECK_FAILED,
                raises=(
                    Raised(
                        title="Release aaaaaaa went live with a check failed",
                        evidence=(("Went live", "2\N{NO-BREAK SPACE}hours ago"),),
                        primary=("Open release", f"/hq/releases/{LIVE}/"),
                    ),
                ),
            ),
        ),
        # Only the release production runs is told of: one replaced since, or one that never went live, is not.
        quiet=(hq(CLEAN, EARLIER_CHECK_FAILED), hq(NOT_DEPLOYED, EARLIER_CLEAN)),
    ),
    Examples(
        "release-server-errors",
        fires=(
            hq(
                CLEAN,
                errors=(
                    ("/infrastructure/topology/", timedelta(hours=1)),
                    ("/infrastructure/topology/", timedelta(minutes=30)),
                    ("/domains/example.com/mail/", timedelta(minutes=10)),
                    ("/search/", timedelta(hours=3)),
                ),
                raises=(
                    Raised(
                        title="3 server errors since release aaaaaaa went live",
                        evidence=(
                            ("/infrastructure/topology/", "2 errors"),
                            ("/domains/example.com/mail/", "1 error"),
                        ),
                        primary=("Open errors", "/errors/"),
                    ),
                ),
            ),
        ),
        # Errors from before it went live, and errors with no release live.
        quiet=(
            hq(CLEAN, errors=(("/search/", timedelta(hours=3)),)),
            hq(NOT_DEPLOYED, errors=(("/search/", timedelta(minutes=5)),)),
        ),
    ),
    Examples(
        "release-pages-heavier",
        fires=(
            hq(
                CLEAN,
                EARLIER_CLEAN,
                kept={
                    LIVE: (
                        # A page query more, and rows more than a tenth heavier.
                        drawn("/search/", queries=21),
                        drawn("/infrastructure/topology/", row_bytes=9_200, html=41_200),
                        *WITHIN[2:],
                    ),
                    EARLIER: BEFORE,
                },
                raises=(
                    Raised(
                        title="2 pages heavier in release aaaaaaa than the release before",
                        evidence=(
                            ("/search/", "Page q 17 from 16"),
                            ("/infrastructure/topology/", "B/row 920 from 800"),
                        ),
                        primary=("Open release", f"/hq/releases/{LIVE}/"),
                    ),
                ),
            ),
        ),
        # Every page within its bars, and a release before with no page measured.
        quiet=(
            hq(CLEAN, EARLIER_CLEAN, kept={LIVE: WITHIN, EARLIER: BEFORE}),
            hq(CLEAN, EARLIER_CLEAN, kept={LIVE: (drawn("/search/", queries=90),), EARLIER: ()}),
            hq(CLEAN, EARLIER_CLEAN, kept={LIVE: (drawn("/search/", queries=90),)}),
        ),
    ),
)

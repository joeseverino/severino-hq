"""A declared workflow's steps as ``{% steps of=... %}`` draws them: under way
with its checks folded, a check failing that opens its fold, one that ran to
its end with a part that failed, and one stopped on the host."""

from datetime import timedelta

from django.utils import timezone

from hq.platform.core.template_parts import Example
lazy from hq.domains.control_plane.observations.github import PROVIDER as GITHUB
lazy from hq.domains.control_plane.observations.host import PROVIDER as HOST
lazy from hq.platform.application.asks import DONE, FAILED, IDLE, QUEUED, RUNNING, Standing
lazy from hq.platform.application.sequences import PERSON, Sequence, Step

_PULL = "https://github.com/example/apps/pull/58"
_DRAWN = '{% section "Example update" %}{% steps of=sequence %}{% endsection %}'


def _waiting(*steps: tuple[str, str]) -> tuple[Step, ...]:
    return tuple(Step(title, Standing(IDLE, word=word), by=HOST) for title, word in steps)


_AFTER = (("Preview", "After you approve"), ("Merge", "After the preview"))
_LATER = (("Apply", "After it merges"), ("Health check", "After it applies"))


def _check(name: str, state: str, word: str, note: str, *, told: bool = False) -> Step:
    url = f"https://github.com/example/apps/actions/runs/{len(name)}"
    return Step(name, Standing(state, note, word=word, url=url), by=GITHUB, told=told)


def _opened() -> Step:
    return Step("Pull request", Standing(DONE, word="#58 opened", url=_PULL), by=GITHUB)


def _checking(*checks: Step) -> dict[str, Sequence]:
    passed = f"{sum(check.done for check in checks)} of {len(checks)} passed"
    failing = any(check.standing.state == FAILED for check in checks)
    standing = Standing(FAILED if failing else RUNNING, passed, word="Failing" if failing else "Running")
    steps = (
        _opened(),
        Step("Checks", standing, checks, by=GITHUB),
        Step("Approve", Standing(IDLE, word="Yours, once checks pass"), by=PERSON),
        *_waiting(*_AFTER, *_LATER),
    )
    return {"sequence": Sequence("example/apps#58", steps)}


def _under_way() -> dict[str, Sequence]:
    """Its checks running, folded, one told by GitHub since the read."""

    return _checking(
        _check("validate", DONE, "Passed", "58 s"),
        _check("scan", RUNNING, "Running", "since 1 minute ago", told=True),
        _check("example-integration-suite-with-a-long-name", QUEUED, "Queued", ""),
    )


def _failing() -> dict[str, Sequence]:
    """A check failing, which opens its fold by itself."""

    return _checking(_check("validate", DONE, "Passed", "58 s"), _check("scan", FAILED, "Failed", "1 min 12 s"))


def _ended() -> dict[str, Sequence]:
    """Every step taken, its apply ended and one app's health check failing after it."""

    since = timezone.now() - timedelta(minutes=4)
    passed = (_check("validate", DONE, "Passed", "58 s"), _check("scan", DONE, "Passed", "1 min 12 s"))
    apps = (
        Step("example-app", Standing(DONE, "just now", word="Healthy"), by=HOST),
        Step("example-worker", Standing(FAILED, "just now", word="Unhealthy"), by=HOST),
    )
    steps = (
        _opened(),
        Step("Checks", Standing(DONE, "2 of 2 passed", word="Passed"), passed, by=GITHUB),
        Step("Approve", Standing(DONE, word="Approved"), by=PERSON),
        Step("Preview", Standing(DONE, since=since, word="Passed", url="/example/runs/1/"), by=HOST),
        Step("Merge", Standing(DONE, word="Done", url=_PULL), by=GITHUB),
        Step("Apply", Standing(DONE, since=since, word="Done", url="/example/runs/2/"), by=HOST),
        Step("Health check", Standing(FAILED, word="Unhealthy"), apps, by=HOST),
    )
    return {"sequence": Sequence("example/apps#58", steps)}


def _failed() -> dict[str, Sequence]:
    """Its preview stopped on the host, said where, the approval the person's again."""

    stopped = "At “Require sudo to let the deploy login start the pull unit” · sudo refuses to start the pull unit."
    checks = (_check("validate", DONE, "Passed", "58 s"),)
    steps = (
        _opened(),
        Step("Checks", Standing(DONE, "1 of 1 passed", word="Passed"), checks, by=GITHUB),
        Step("Approve", Standing(IDLE, word="Yours to approve"), by=PERSON),
        Step("Preview", Standing(FAILED, stopped, word="Failed", url="/example/runs/1/"), by=HOST),
        *_waiting(_AFTER[1], *_LATER),
    )
    return {"sequence": Sequence("example/apps#58", steps)}


STATES = {
    "sequence": Example(_DRAWN, _under_way),
    "sequence-failing": Example(_DRAWN, _failing),
    "sequence-ended": Example(_DRAWN, _ended),
    "sequence-failed": Example(_DRAWN, _failed),
}

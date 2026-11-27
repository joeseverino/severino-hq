"""A declared workflow's steps as ``{% steps of=... %}`` draws them: one under
way, a check of it told by an event since the read, and one that ran to its
end with a part that failed."""

from datetime import timedelta

from django.utils import timezone

from hq.platform.core.template_parts import Example
lazy from hq.platform.application.asks import DONE, FAILED, IDLE, QUEUED, RUNNING, Standing
lazy from hq.platform.application.sequences import By, Sequence, Step

_PULL = "https://github.com/example/apps/pull/58"


def _waiting(*titles: str) -> tuple[Step, ...]:
    return tuple(Step(title, Standing(IDLE, word="Waiting"), by=By.HOST) for title in titles)


def _check(name: str, state: str, word: str, note: str, *, told: bool = False) -> Step:
    url = f"https://github.com/example/apps/actions/runs/{len(name)}"
    return Step(name, Standing(state, note, word=word, url=url), by=By.GITHUB, told=told)


def _opened() -> Step:
    title = "Update example/app Docker tag to v1.2.1"
    return Step("Pull request", Standing(DONE, title, word="#58 open", url=_PULL), by=By.RENOVATE)


def _under_way() -> dict[str, Sequence]:
    """Its checks running, one told by GitHub since the read, the approval the person's next."""

    checks = (
        _check("validate", DONE, "Passed", "58 s"),
        _check("scan", RUNNING, "Running", "since 1 minute ago", told=True),
        _check("example-integration-suite-with-a-long-name", QUEUED, "Queued", ""),
    )
    steps = (
        _opened(),
        Step("Checks", Standing(RUNNING, word="Running"), checks, by=By.GITHUB),
        Step("Approve", Standing(IDLE, word="Yours to approve"), by=By.YOU),
        *_waiting("Preview", "Merge", "Apply", "Health check"),
    )
    return {"sequence": Sequence("example/apps#58", steps)}


def _ended() -> dict[str, Sequence]:
    """Every step taken, its apply ended and one app's health check failing after it."""

    since = timezone.now() - timedelta(minutes=4)
    passed = (_check("validate", DONE, "Passed", "58 s"), _check("scan", DONE, "Passed", "1 min 12 s"))
    apps = (
        Step("example-app", Standing(DONE, "just now", word="Healthy"), by=By.HOST),
        Step("example-worker", Standing(FAILED, "just now", word="Unhealthy"), by=By.HOST),
    )
    steps = (
        _opened(),
        Step("Checks", Standing(DONE, word="Passed"), passed, by=By.GITHUB),
        Step("Approve", Standing(DONE, word="Approved"), by=By.YOU),
        Step("Preview", Standing(DONE, since=since, word="Previewed", url="/example/runs/1/"), by=By.HOST),
        Step("Merge", Standing(DONE, word="Merged", url=_PULL), by=By.GITHUB),
        Step("Apply", Standing(DONE, since=since, word="Applied", url="/example/runs/2/"), by=By.HOST),
        Step("Health check", Standing(FAILED, word="Unhealthy"), apps, by=By.HOST),
    )
    return {"sequence": Sequence("example/apps#58", steps)}


def _failed() -> dict[str, Sequence]:
    """Its preview stopped on the host, said where, the approval the person's again."""

    stopped = "At “Require sudo to let the deploy login start the pull unit” · sudo refuses to start the pull unit."
    steps = (
        _opened(),
        Step("Checks", Standing(DONE, word="Passed"), (_check("validate", DONE, "Passed", "58 s"),), by=By.GITHUB),
        Step("Approve", Standing(IDLE, word="Yours to approve"), by=By.YOU),
        Step("Preview", Standing(FAILED, stopped, word="Preview failed", url="/example/runs/1/"), by=By.HOST),
        *_waiting("Merge", "Apply", "Health check"),
    )
    return {"sequence": Sequence("example/apps#58", steps)}


STATES = {
    "sequence": Example('{% section "Example update" %}{% steps of=sequence %}{% endsection %}', _under_way),
    "sequence-ended": Example('{% section "Example update" %}{% steps of=sequence %}{% endsection %}', _ended),
    "sequence-failed": Example('{% section "Example update" %}{% steps of=sequence %}{% endsection %}', _failed),
}

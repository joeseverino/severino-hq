"""An app update's way from a newer release to running it, as its card
lists it in order (``sequences.Step``): the pull request Renovate opens, its
checks, the approval that merges it, the apply on its host and the health
check after it. Each step is done, under way, failed or waiting, said from
what HQ stores of it, so the one press the card offers is the next step's."""

from django.utils.text import capfirst

from .app_updates import UpdateKind, UpdateState, of_app
from .asks import DONE, FAILED, IDLE, RUNNING, Standing
from .card_items import Answer
from .github_estate import check_said
from .labels import dotted
from .moments import when
from .sequences import Step
from .tone import Tone

# How a step stands, by the tone its answer is said in.
_STATES = {Tone.GOOD: DONE, Tone.ATTENTION: RUNNING, Tone.SERIOUS: FAILED}
# The checks as a whole, by the summary state the read gives them.
_CHECKS = {"success": ("Passed", DONE), "pending": ("Running", RUNNING), "failure": ("Failing", FAILED)}


def update_flow(state: UpdateState) -> tuple[Step, ...]:
    """Every step of ``state``'s update in order, each as it stands."""

    merged = state.kind == UpdateKind.MERGED
    # An approval whose merge landed says how its apply stands, before the pull request is read closed.
    landed = state.kind == UpdateKind.OPEN and state.beside is not None and state.beside.merged
    behind = merged or landed
    return (
        _opened(state) if merged or state.update is not None else _said("Pull request", state.answer),
        _checks(state) if state.update else _waiting("Checks", "Once it opens", done="Done" if merged else ""),
        _decided("Approve", state)
        if state.kind == UpdateKind.OPEN and not landed
        else _waiting("Approve", "Once its checks pass", done="Merged" if behind else ""),
        _decided("Apply", state) if behind else _waiting("Apply", "Once it merges"),
        _health(state) if behind else _waiting("Health check", "Once it applies"),
    )


def _said(title: str, answer: Answer, *, url: str = "") -> Step:
    """A step as an answer says it: its word, its reasons as its sentence."""

    reasons = dotted(*(line for line in answer.because if isinstance(line, str)))
    return Step(title, Standing(_STATES.get(answer.tone, IDLE), note=reasons, word=answer.short, url=url))


def _waiting(title: str, until: str, *, done: str = "") -> Step:
    """A step before the one under way, waiting ``until``; or behind it, ``done`` saying how it ended."""

    return Step(title, Standing(DONE, word=done) if done else Standing(IDLE, note=until, word="Waiting"))


def _opened(state: UpdateState) -> Step:
    update = state.update
    if update is None:
        where = f"In {state.managed.repository}" if state.managed else ""
        return Step("Pull request", Standing(DONE, note=where, word="Merged"))
    return Step("Pull request", Standing(DONE, note=update.title, word=f"{update.label} open", url=update.url))


def _checks(state: UpdateState) -> Step:
    update = state.update
    said = update.checks_said
    word, at = _CHECKS.get(str(update.checks.get("state") or ""), ("None reported", IDLE))
    parts = tuple(map(check_said, update.check_runs))
    return Step("Checks", Standing(at, note=said.why or ("" if parts else said.badge), word=word), parts)


def _decided(title: str, state: UpdateState) -> Step:
    """The step whose press the card offers: where it stands once asked,
    else the person's to take, else waiting on what the card says beside it."""

    if (standing := state.beside) is not None:
        return _said(title, standing.answer, url=standing.run_url)
    return Step(title, Standing(IDLE, word=f"Yours to {title.lower()}" if state.action else "Waiting"))


def _health(state: UpdateState) -> Step:
    """What each container it moves says of its own health once its apply
    ended well, as HQ last read it; waiting until a read after it ended."""

    standing = state.beside
    operation = standing.operation if standing is not None and standing.tone == Tone.GOOD else None
    since = operation.completed_at if operation is not None else None
    if since is None:
        return _waiting("Health check", "Once it applies")
    items = state.update.containers if state.update else of_app(state.managed.repository, state.managed.app)
    read = [item for item in items if item.running.observed_at and item.running.observed_at > since]
    if not read or len(read) < len(items):
        return _waiting("Health check", "Once HQ reads it after the apply")
    checks = [(item, item.running.check) for item in read if item.running.check is not None]
    if not checks:
        return Step("Health check", Standing(DONE, word="None declared"))
    parts = tuple(f"{item.running.name} {check.value} {when(item.running.observed_at)}" for item, check in checks)
    worst = next((check for _item, check in checks if check.tone != Tone.GOOD), None)
    if worst is not None:
        return Step("Health check", Standing(FAILED, word=capfirst(worst.value)), parts)
    return Step("Health check", Standing(DONE, word="Healthy"), parts)

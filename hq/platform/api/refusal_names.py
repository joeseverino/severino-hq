"""The names the API document gives its refusals, from the codes each answers alone.

A kind of call names the set of codes it answers at a status, so two
operations refused alike share one response and no order of routes moves a
name.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from http import HTTPStatus
from itertools import product

from hq.platform.application.refusal_status import LATER_CODES, REFUSAL_STATUS, call_doors

from . import views
from .fragments import OpenAPIError, pascal


def codes_by_status(codes: Iterable[str]) -> dict[int, list[str]]:
    """Each status ``codes`` answer at, with the codes answered there, by REFUSAL_STATUS."""

    statuses: dict[int, list[str]] = {}
    for code in sorted(codes):
        statuses.setdefault(REFUSAL_STATUS[code], []).append(code)
    return dict(sorted(statuses.items()))


LATER_STATUSES = frozenset(codes_by_status(LATER_CODES))


@dataclass(frozen=True, slots=True)
class CallKind:
    """A kind of call whose codes the document names a refusal for: its words, by axis, and its codes.

    A call by any name is ``general``: it may be any capability's call, so it
    names a set only where no particular kind answers exactly that set.
    """

    words: tuple[tuple[str, str], ...]
    codes: frozenset[str]
    general: bool = False

    def at(self, status: int) -> frozenset[str]:
        return frozenset(codes_by_status(self.codes).get(status, ()))


# The axes a kind's words come in, in the order a name says them. A call's
# subject word, "Call", is said only when no other word is.
_AXES = ("target", "check", "effect", "subject")
_CALL = ("subject", "Call")


@cache
def call_kinds() -> tuple[CallKind, ...]:
    """Every kind of call the API answers beyond what every endpoint does: a
    resource read, a capability's call by what it declares, and a call by any name."""

    calls = tuple(
        CallKind(
            (
                ("target", "Targeted" if targeted else "Untargeted"),
                ("check", "StaleChecked" if stale_write else "Unchecked"),
                ("effect", "Change" if changes else "Read"),
                _CALL,
            ),
            views.call_codes(call_doors(targeted=targeted, stale_write=stale_write, changes=changes)),
        )
        for targeted, stale_write, changes in product((True, False), repeat=3)
    )
    resource = CallKind((("subject", "ResourceRead"),), views.ENDPOINT_CODES | views.RESOURCE_CODES)
    return (*calls, resource, CallKind((("subject", "AnyCall"),), views.execute_codes(None), general=True))


def refusal_name(status: int, codes: list[str]) -> str:
    """The component name for refusing at ``status`` with ``codes``, from them alone.

    The set every endpoint answers there is named for the status; one code at
    a status no endpoint answers, for that code; any other set, for the kind
    of call that answers exactly it: the words the kinds answering it share,
    then the status. Two operations answering the same codes so share one
    response, and no order of routes moves a name. A set no kind answers fails
    the build here, and a name two sets reach fails it in ``refusal``.
    """

    phrase = pascal(HTTPStatus(status).phrase)
    answered = frozenset(codes)
    shared = frozenset(codes_by_status(views.ENDPOINT_CODES).get(status, ()))
    if answered == shared:
        return phrase
    if len(answered) == 1 and not shared:
        return pascal(codes[0])
    said = _kind_words(status, answered)
    if not said:
        raise OpenAPIError(
            f"No one kind of call answers {status} with exactly {sorted(answered)}, so its refusal has no name: "
            "declare it as a kind in refusal_names.call_kinds."
        )
    return "".join(said) + phrase


def _kind_words(status: int, answered: frozenset[str]) -> list[str]:
    """The words every kind answering ``status`` with exactly ``answered`` shares, in ``_AXES`` order."""

    kinds = [kind for kind in call_kinds() if kind.at(status) == answered]
    named = [kind for kind in kinds if not kind.general] or kinds
    if not named:
        return []
    common = {word for word in named[0].words if all(word in kind.words for kind in named[1:])}
    said = sorted(common, key=lambda word: _AXES.index(word[0]))
    if said != [_CALL]:
        said = [word for word in said if word != _CALL]
    return [word for _, word in said]

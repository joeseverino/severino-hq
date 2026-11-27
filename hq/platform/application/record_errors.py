"""The ways a use case refuses: not there, a clash, forbidden by policy or
state, unusable material, or a value HQ cannot act on.

An adapter answers each one way, by its ``code``. A ``reason`` is a
``Sentence``, written by the host and never taken from a request or a
dependency, since it is the only text an adapter answers with.
"""

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, ClassVar

from .refusal_codes import Code
lazy from .refusal_status import RefusalDetails

# A sentence HQ wrote: a ``str``, which HQ's mypy reads as ``Written`` and
# holds every value passed for one to (tools/lint/mypy_sentence.py).
Sentence = str

if TYPE_CHECKING:

    class Written(str):
        """What HQ's mypy reads ``Sentence`` as."""


def worded(text: Sentence) -> Sentence:
    """``text``, as the sentence HQ's mypy holds it to be, for a value built in a function's body."""

    return text


class HostRefusal(Exception):
    """A refusal whose ``reason`` every adapter may show as it stands."""

    code: ClassVar[Code]

    def __init__(self, reason: Sentence, *, details: RefusalDetails | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        # What gets past the refusal, as the envelope's ``details`` carries it.
        self.details = details


class RecordNotFound(HostRefusal, LookupError):
    """The record the request names does not exist, or the caller cannot see it."""

    code = Code.NOT_FOUND


def one_of[T](items: Iterable[T], matches: Callable[[T], bool], missing: Sentence) -> T:
    """The first of a read's ``items`` that ``matches``, or the refusal that none does."""

    for item in items:
        if matches(item):
            return item
    raise RecordNotFound(missing)


class RecordConflict(HostRefusal):
    """The write would clash with a record that already exists or has moved on."""

    code = Code.CONFLICT


class PolicyError(HostRefusal, ValueError):
    """The request is valid in shape, and HQ's settings or the record's state refuse it."""

    code = Code.PRECONDITION


class CertificateError(HostRefusal, ValueError):
    """The uploaded material is not a usable certificate and key pair."""

    code = Code.INVALID_MATERIAL


class InputError(HostRefusal, ValueError):
    """A value of the right type that HQ cannot act on, said in the host's words."""

    code = Code.INVALID_INPUT

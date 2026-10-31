"""The ways a use case refuses: the record is not there, it clashes, policy
or state forbids the change, the uploaded material cannot be used, or a value
the command's type admits is still not one HQ can act on.

Every domain raises these, so an adapter answers each one way, by its ``code``.
A ``reason`` is written by the host, never taken from a request or a
dependency, and is the only text of theirs an adapter answers with. It is a
literal where it is raised, or a ``Sentence`` carried from where one is
written, and its dynamic parts are values HQ wrote: a constant, or a day, a
count or a size in HQ's own form. ``RefusalTextTests`` reads every refusal for
it (``tests/refusal_text.py`` says what a sentence is).
"""

from typing import ClassVar

# A sentence HQ wrote. To the type checker a ``str``; to the refusal test a
# promise: every value passed, defaulted or returned where it is annotated is
# held to be one, so a refusal may be built from it.
Sentence = str


class HostRefusal(Exception):
    """A refusal whose ``reason`` every adapter may show as it stands."""

    code: ClassVar[str]

    def __init__(self, reason: Sentence) -> None:
        super().__init__(reason)
        self.reason = reason


class RecordNotFound(HostRefusal, LookupError):
    """The record the request names does not exist, or the caller cannot see it."""

    code = "not_found"


class RecordConflict(HostRefusal):
    """The write would clash with a record that already exists or has moved on."""

    code = "conflict"


class PolicyError(HostRefusal, ValueError):
    """The request is valid in shape, and HQ's settings or the record's state refuse it."""

    code = "precondition"


class CertificateError(HostRefusal, ValueError):
    """The uploaded material is not a usable certificate and key pair."""

    code = "invalid_material"


class InputError(HostRefusal, ValueError):
    """A value of the right type that HQ cannot act on, said in the host's words."""

    code = "invalid_input"

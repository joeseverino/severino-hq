"""The ways a use case refuses: not there, a clash, forbidden by policy or
state, unusable material, or a value HQ cannot act on.

An adapter answers each one way, by its ``code``. A ``reason`` is written by
the host, never taken from a request or a dependency, since it is the only text
an adapter answers with (``RefusalTextTests``).
"""

from typing import ClassVar

lazy from .refusal_status import RefusalDetails

# A sentence HQ wrote. To the type checker a ``str``; to the refusal test a
# promise: every value passed, defaulted or returned where it is annotated is
# held to be one, so a refusal may be built from it.
Sentence = str

# The code of a refusal for a record that is not there. A field's
# ``ValidationError`` carries it where the field names a record (a model's
# reference column), so the call is answered as ``RecordNotFound`` is.
NOT_FOUND = "not_found"


class HostRefusal(Exception):
    """A refusal whose ``reason`` every adapter may show as it stands."""

    code: ClassVar[str]

    def __init__(self, reason: Sentence, *, details: RefusalDetails | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        # What gets past the refusal, as the envelope's ``details`` carries it.
        self.details = details


class RecordNotFound(HostRefusal, LookupError):
    """The record the request names does not exist, or the caller cannot see it."""

    code = NOT_FOUND


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

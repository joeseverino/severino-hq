"""The two ways a use case refuses a record: it is not there, or it clashes.

Every domain raises these rather than declaring its own, so an adapter answers
them one way: ``not_found`` and ``conflict``. ``reason`` is written by the
host, never taken from a request or a dependency, and is the only text of
theirs an adapter answers with.
"""


class _Refusal(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RecordNotFound(_Refusal, LookupError):
    """The record the request names does not exist, or the caller cannot see it."""


class RecordConflict(_Refusal):
    """The write would clash with a record that already exists or has moved on."""

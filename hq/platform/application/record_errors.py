"""The two ways a use case refuses a record: it is not there, or it clashes.

Every domain raises these rather than declaring its own, so an adapter answers
them one way: ``not_found`` and ``conflict``. Their text is written by the
host, never taken from a request or a dependency, so it may reach the caller.
"""


class RecordNotFound(LookupError):
    """The record the request names does not exist, or the caller cannot see it."""


class RecordConflict(Exception):
    """The write would clash with a record that already exists or has moved on."""

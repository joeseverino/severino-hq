"""Form views whose write is one application service call.

``ServiceCreateMixin``, ``ServiceUpdateMixin`` and ``ServiceDeleteMixin`` write
a record with a URL of its own and go to it; ``CommandFormMixin`` posts a
command from a page that shows itself again, a domain's refusal on the form.
The host and every extension share these steps rather than restate them.
"""

from hq.platform.application.writes import (
    CommandFormMixin,
    ServiceCreateMixin,
    ServiceDeleteMixin,
    ServiceUpdateMixin,
    ServiceWriteMixin,
    WriteMessages,
)

__all__ = [
    "CommandFormMixin",
    "ServiceCreateMixin",
    "ServiceDeleteMixin",
    "ServiceUpdateMixin",
    "ServiceWriteMixin",
    "WriteMessages",
]

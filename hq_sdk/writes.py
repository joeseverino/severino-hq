"""Form views whose write is one capability, run through the runner.

``CapabilityFormMixin`` creates or updates, and ``CapabilityCommandMixin`` posts
a command from a page that shows itself again. Neither can write but through the
runner, and what a write says next is its answer's ``message``.
"""

from hq.platform.application.writes import CapabilityCommandMixin, CapabilityFormMixin

__all__ = ["CapabilityCommandMixin", "CapabilityFormMixin"]

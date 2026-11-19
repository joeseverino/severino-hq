"""Form views whose write is one capability, run through the runner.

``CapabilityFormMixin`` creates or updates, ``CapabilityDeleteMixin`` deletes,
and ``CapabilityCommandMixin`` posts a command from a page that shows itself
again. None can write but through the runner, and what a write says next is its
answer's ``message``.
"""

from hq.platform.application.writes import CapabilityCommandMixin, CapabilityDeleteMixin, CapabilityFormMixin

__all__ = ["CapabilityCommandMixin", "CapabilityDeleteMixin", "CapabilityFormMixin"]

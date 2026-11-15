"""Form views whose write is one capability, run through the runner.

``CapabilityFormMixin`` creates or updates a record from its ``ModelForm`` and
goes to it, ``CapabilityDeleteMixin`` deletes one and goes back to the list,
and ``CapabilityCommandMixin`` posts a command from a page that shows itself
again, a refusal on the form. Each names the capabilities it runs, and has no
way to write but the runner. What a write says on the next page is its
answer's ``message``, never the view's.
"""

from hq.platform.application.writes import CapabilityCommandMixin, CapabilityDeleteMixin, CapabilityFormMixin

__all__ = ["CapabilityCommandMixin", "CapabilityDeleteMixin", "CapabilityFormMixin"]

"""Form views whose write is one application service call.

The list side of a page has been declarative for a while -- a view names its
filters and sorts and ``TableListMixin`` derives the rest. The write side was
not, so every create, update, delete and command restated the same steps by
hand, in the host and in every extension, and the same steps could drift once
per domain.

Two shapes, because there are two:

``ServiceCreateMixin`` / ``ServiceUpdateMixin`` / ``ServiceDeleteMixin``
    A record with a URL of its own. Build a command from the cleaned data, call
    the service, reload what the service named, announce it, go there. Update
    reloads by the identity the service *returned*, so renaming a record
    redirects to where it now lives instead of 404ing on where it used to.

``CommandFormMixin``
    A command posted from a page that then shows itself again -- the shape
    extensions mostly have. Adds one thing worth sharing: a service raising
    ``ValueError`` is the domain saying no, and that belongs on the form beside
    the field it concerns rather than on a 500 page.

    from hq_sdk.writes import CommandFormMixin

    class ThresholdView(CommandFormMixin, CapabilityRequiredMixin, FormView):
        required_capability = "example.write"
        form_class = ThresholdForm
        service = staticmethod(save_threshold)
        command = ThresholdCommand
        success_url_name = "example:thresholds"
        success_message = "Thresholds updated."

Declare the service with ``staticmethod``. A plain function on a class becomes
a bound method on access and would receive the view as its command; the mixins
raise at class-creation time rather than letting that reach a write.
"""

from application.writes import (
    CommandFormMixin,
    ServiceCreateMixin,
    ServiceDeleteMixin,
    ServiceUpdateMixin,
    ServiceWriteMixin,
)

__all__ = [
    "CommandFormMixin",
    "ServiceCreateMixin",
    "ServiceDeleteMixin",
    "ServiceUpdateMixin",
    "ServiceWriteMixin",
]

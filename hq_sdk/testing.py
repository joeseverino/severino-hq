"""Conformance helpers for plugin test suites."""

from application.demo import demo_scope
from application.plugin_testing import (
    ComposedPluginTestCase,
    sibling,
    undefined_style_classes,
)
from core.models import AuditLog
from hq_sdk.validation import unsupported_hq_imports


def audit_writer():
    """The manager audit rows are written through, for tests that break it.

    ``hq_sdk.audit`` deliberately withholds the host's audit model: an
    extension records events through ``record_event`` and reads them through
    ``audit_events``, and neither needs the model itself.

    Simulating the *write* failing is the exception. An extension that must
    fail closed when its audit row cannot commit has to be able to prove it,
    and that means patching the thing the host writes with. Exposed here rather
    than from ``hq_sdk.audit`` so it is unavailable to anything but a test
    suite, and named for what it is rather than for the model behind it.
    """

    return AuditLog.objects


# Entering the substituting scope is a test affordance, not part of the
# contract: production turns it on from the operator's session and a domain
# only ever reads it. Exposed here so an extension can prove what its own
# surfaces do under a demo, and nowhere an extension could switch it on for a
# real request.

__all__ = [
    "ComposedPluginTestCase",
    "audit_writer",
    "demo_scope",
    "sibling",
    "undefined_style_classes",
    "unsupported_hq_imports",
]

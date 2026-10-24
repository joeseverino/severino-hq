"""Conformance helpers for plugin test suites."""

import re
import time

from django.db.models import Manager

from hq.domains.jobs.testing import held_jobs
from hq.platform.application.demo import demo_scope
from hq.platform.application.plugin_testing import (
    ComposedPluginTestCase,
    providers_derived_again,
    sibling,
    undefined_style_classes,
)
from hq.platform.core.interface_text import unclosed_template_comments
from hq.platform.core.models import AuditLog
from hq.platform.core.outbound import OutboundInRequest
from hq_sdk.validation import unsupported_hq_imports


def audit_writer() -> Manager[AuditLog]:
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


def reaches_out() -> None:
    """What a network call is to HQ's rule, for the double that stands in for one.

    A test replaces an extension's network client with a double, and a double
    opens no connection, so the rule that refuses a request reaching out has
    nothing to refuse and the test proves nothing. Called from the double,
    this raises ``OutboundInRequest`` when a request is being served and does
    nothing inside a job, exactly as the real call would.

        with held_jobs() as held:
            response = self.client.post(url)      # answers; the double is untouched
            held.run()                            # the job runs it
    """

    time.sleep(0)


_NO_BREAK = re.compile(r"[\u00a0\u202f]|&nbsp;|&#160;|&#x0*a0;", re.IGNORECASE)
_JOINER = re.compile(r"\u2060|&#8288;|&#x0*2060;", re.IGNORECASE)


def plain(text: str | bytes) -> str:
    """Text with the host's no-break spaces read as spaces, and its word joiners as nothing.

    The host keeps a number with its unit ("2\u00a0weeks") or its sign
    ("\u2212\u2060$5.00") and may change where it does. A test comparing
    words it chose against what a page or a value says reads both through
    this, so that choice is never its to pin:

        self.assertIn("Renewed for 2 weeks", plain(response.content))
    """

    if isinstance(text, bytes):
        text = text.decode("utf-8")
    return _JOINER.sub("", _NO_BREAK.sub(" ", text))


# Entering the substituting scope is a test affordance, not part of the
# contract: production turns it on from the operator's session and a domain
# only ever reads it. Exposed here so an extension can prove what its own
# surfaces do under a demo, and nowhere an extension could switch it on for a
# real request.

__all__ = [
    "ComposedPluginTestCase",
    "OutboundInRequest",
    "audit_writer",
    "demo_scope",
    "held_jobs",
    "plain",
    "providers_derived_again",
    "reaches_out",
    "sibling",
    "unclosed_template_comments",
    "undefined_style_classes",
    "unsupported_hq_imports",
]

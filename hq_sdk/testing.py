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

    ``hq_sdk.audit`` withholds the host's audit model, but an extension that
    fails closed when its audit row cannot commit must be able to prove it by
    patching what the host writes with; offered here, to test suites alone.
    """

    return AuditLog.objects


def reaches_out() -> None:
    """What a network call is to HQ's rule, for the double that stands in for one.

    A double opens no connection, so without this a test of a view that reaches
    out proves nothing. Called from the double, it raises ``OutboundInRequest``
    while a request is served and does nothing inside a job, as the real call.
    """

    # A sleep is what the rule refuses in a request and allows in a job.
    time.sleep(0)


_NO_BREAK = re.compile(r"[\u00a0\u202f]|&nbsp;|&#160;|&#x0*a0;", re.IGNORECASE)
_JOINER = re.compile(r"\u2060|&#8288;|&#x0*2060;", re.IGNORECASE)


def plain(text: str | bytes) -> str:
    """Text with the host's no-break spaces read as spaces, and its word joiners as nothing.

    The host keeps a number with its unit ("2\u00a0weeks") or its sign
    ("\u2212\u2060$5.00") and may change where it does, so a test reads both
    its own words and the page's through this, and never pins that choice.
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

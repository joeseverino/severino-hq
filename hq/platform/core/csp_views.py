"""The endpoint browsers report Content Security Policy violations to."""

import json
from datetime import timedelta

from django.contrib.auth.decorators import login_not_required
from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .audit import record_event
from .models import AuditLog
from .network import client_ip

# The browser's own account of a policy it refused to follow. Bounded on every
# axis a stranger controls: how much it may send, how much of that is kept, and
# how often the same complaint may reach the database.
_CSP_REPORT_MAX_BYTES = 8 * 1024
_CSP_FIELD_LIMIT = 200
_CSP_REPEAT_WINDOW_SECONDS = 3600


def _csp_violations(payload):
    """The violations in a report body, whichever of the two shapes it uses.

    `application/csp-report` sends one violation under a `csp-report` key;
    the Reporting API sends a list of report objects with the violation under
    `body`. Both are read, because which one arrives depends on the browser
    and neither is worth losing.
    """

    if isinstance(payload, dict):
        report = payload.get("csp-report")
        return [report] if isinstance(report, dict) else []
    if isinstance(payload, list):
        return [
            item["body"]
            for item in payload
            if isinstance(item, dict) and isinstance(item.get("body"), dict)
        ]
    return []


@login_not_required
@csrf_exempt
@require_POST
def csp_report(request):
    """Record a Content-Security-Policy violation the browser refused to run.

    The policy is the one boundary HQ cannot verify from the inside: it is
    enforced in someone else's browser, and until the browser says so, a
    directive that is quietly failing looks exactly like a directive that is
    quietly working. This is where it says so.

    Unauthenticated by necessity: a violation report is sent without
    credentials, so requiring a session would silence reports from the sign-in
    page, which is the page where one would matter most. It is still behind
    the network gate, still CSRF-exempt only for a body it never trusts, and
    it answers the same 204 whatever it decides, so nothing here is an oracle.
    """


    if len(request.body) > _CSP_REPORT_MAX_BYTES:
        return HttpResponse(status=204)
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return HttpResponse(status=204)

    for violation in _csp_violations(payload)[:10]:
        directive = str(
            violation.get("effective-directive")
            or violation.get("effectiveDirective")
            or violation.get("violated-directive")
            or ""
        )[:_CSP_FIELD_LIMIT]
        blocked = str(
            violation.get("blocked-uri") or violation.get("blockedURL") or ""
        )[:_CSP_FIELD_LIMIT]
        document = str(
            violation.get("document-uri") or violation.get("documentURL") or ""
        )[:_CSP_FIELD_LIMIT]
        if not directive:
            continue
        # One row per distinct complaint per hour. A page that violates the
        # policy on every load would otherwise write a row on every load, and
        # the thousandth copy says nothing the first did not.
        since = timezone.now() - timedelta(seconds=_CSP_REPEAT_WINDOW_SECONDS)
        already = AuditLog.objects.filter(
            action=AuditLog.Action.FAILED,
            object_type="ContentSecurityPolicy",
            metadata__directive=directive,
            metadata__blocked=blocked,
            created_at__gte=since,
        ).exists()
        if already:
            continue
        record_event(
            action=AuditLog.Action.FAILED,
            type_label="ContentSecurityPolicy",
            message=f"The browser refused {blocked or 'a resource'} under {directive}.",
            metadata={
                "directive": directive,
                "blocked": blocked,
                "document": document,
                "source": client_ip(request),
            },
        )
    return HttpResponse(status=204)

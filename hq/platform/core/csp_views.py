"""The endpoint browsers report Content Security Policy violations to."""

import json
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.http import HttpResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .audit import record_event
from .models import AuditLog
from .network import client_ip
from .write_bounds import RowCap, writes

# The browser's own account of a policy it refused to follow. Bounded on every
# axis a stranger controls: how much it may send, which values of it are kept,
# how often the same complaint may reach the database, and how many rows every
# complaint together may write.
_CSP_REPORT_MAX_BYTES = 8 * 1024
_CSP_FIELD_LIMIT = 200
_CSP_REPEAT_WINDOW_SECONDS = 3600
_LABEL = "ContentSecurityPolicy"

CSP_REPORTS = RowCap(
    "policy reports",
    "at most 60 rows an hour, of a directive HQ's policy governs and a blocked origin or keyword",
    action=AuditLog.Action.FAILED,
    type_label=_LABEL,
    rows=60,
    window=timedelta(hours=1),
)

# Each directive a violation can name, and the directives it falls back to when
# the policy does not declare it (CSP Level 3, 6.8.3 "Get the fallback list";
# 6.1.3: every fetch directive falls back to default-src). A violation names its
# effective directive, which is often a more specific one than the policy wrote
# (script-src-elem for a policy of script-src).
_FALLBACK = {
    "script-src-elem": ("script-src", "default-src"),
    "script-src-attr": ("script-src", "default-src"),
    "style-src-elem": ("style-src", "default-src"),
    "style-src-attr": ("style-src", "default-src"),
    "worker-src": ("child-src", "script-src", "default-src"),
    "frame-src": ("child-src", "default-src"),
    "child-src": ("default-src",),
    "script-src": ("default-src",),
    "style-src": ("default-src",),
    "connect-src": ("default-src",),
    "font-src": ("default-src",),
    "img-src": ("default-src",),
    "manifest-src": ("default-src",),
    "media-src": ("default-src",),
    "object-src": ("default-src",),
}
# Where reports go, never what a resource is refused under.
_REPORTING = frozenset({"report-to", "report-uri"})

# What a violation's blocked resource is when it is not a URL (CSP Level 3, 2.4:
# "inline", "eval", "wasm-eval", "trusted-types-policy", "trusted-types-sink"),
# what the legacy report-uri body says for the page's own origin ("self"), and
# the schemes a non-HTTP(S) URL is reduced to (5.4: such a URL is reported as
# its scheme alone).
_KEYWORDS = frozenset(
    {
        "inline",
        "eval",
        "wasm-eval",
        "trusted-types-policy",
        "trusted-types-sink",
        "self",
        "data",
        "blob",
        "filesystem",
        "about",
        "javascript",
        "ws",
        "wss",
        "chrome-extension",
        "moz-extension",
        "safari-extension",
        "safari-web-extension",
    }
)


def _governed() -> frozenset[str]:
    """The directive names a violation of HQ's policy can carry."""

    declared = set(settings.SECURE_CSP) | set(settings.SECURE_CSP_REPORT_ONLY)
    falling = {name for name, chain in _FALLBACK.items() if declared & set(chain)}
    return frozenset((declared - _REPORTING) | falling)


def _directive(violation: dict) -> str:
    """The violation's directive when HQ's policy governs it, else ""."""

    named = str(
        violation.get("effective-directive")
        or violation.get("effectiveDirective")
        or violation.get("violated-directive")
        or ""
    )[:_CSP_FIELD_LIMIT]
    # The legacy violated-directive carries the directive's value after its name.
    name = named.split(maxsplit=1)[0].lower() if named.strip() else ""
    return name if name in _governed() else ""


def _blocked(violation: dict) -> str:
    """What was blocked, as an origin or a keyword, or "" for anything else."""

    value = str(violation.get("blocked-uri") or violation.get("blockedURL") or "")[:_CSP_FIELD_LIMIT].strip()
    if value.lower() in _KEYWORDS:
        return value.lower()
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return ""
    scheme = parts.scheme.lower()
    if scheme in _KEYWORDS:
        return scheme
    if scheme not in ("http", "https") or not parts.hostname:
        return ""
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{scheme}://{host}" + (f":{port}" if port is not None else "")


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
        return [item["body"] for item in payload if isinstance(item, dict) and isinstance(item.get("body"), dict)]
    return []


# Still behind the network gate, and CSRF-exempt only for a body it never trusts.
@writes(CSP_REPORTS)
@login_not_required
@csrf_exempt
@require_POST
def csp_report(request):
    """Record a Content-Security-Policy violation the browser refused to run.

    The policy is enforced in someone else's browser, so a directive quietly
    failing looks like one quietly working until the browser says so. A report
    comes without credentials, and the sign-in page is where one matters most,
    so this needs no session; it answers 204 whatever it decides, so nothing
    here is an oracle.
    """

    if len(request.body) > _CSP_REPORT_MAX_BYTES:
        return HttpResponse(status=204)
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except ValueError, UnicodeDecodeError:
        return HttpResponse(status=204)

    for violation in _csp_violations(payload)[:10]:
        directive = _directive(violation)
        if not directive:
            continue
        blocked = _blocked(violation)
        document = str(violation.get("document-uri") or violation.get("documentURL") or "")[:_CSP_FIELD_LIMIT]
        # One row per distinct complaint per hour. A page that violates the
        # policy on every load would otherwise write a row on every load, and
        # the thousandth copy says nothing the first did not.
        since = timezone.now() - timedelta(seconds=_CSP_REPEAT_WINDOW_SECONDS)
        already = AuditLog.objects.filter(
            action=AuditLog.Action.FAILED,
            object_type=_LABEL,
            metadata__directive=directive,
            metadata__blocked=blocked,
            created_at__gte=since,
        ).exists()
        if already or not CSP_REPORTS.admits():
            continue
        record_event(
            action=AuditLog.Action.FAILED,
            type_label=_LABEL,
            message=f"The browser refused {blocked or 'a resource'} under {directive}.",
            metadata={
                "directive": directive,
                "blocked": blocked,
                "document": document,
                "source": client_ip(request),
            },
        )
    return HttpResponse(status=204)

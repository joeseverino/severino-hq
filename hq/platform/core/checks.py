"""System checks on a deployment's settings, run by ``manage.py check --deploy``.

A system check runs before every management command, so nothing here reads the
source tree: a rule about source is an architecture test, which a gate runs
once (``hq.platform.core.interface_text`` holds the ones about wording).
"""

from __future__ import annotations

from django.conf import settings
from django.core import checks


@checks.register(checks.Tags.security, deploy=True)
def deployment_identity_is_set(app_configs=None, **kwargs):
    """A deployment names its own host and sign-in issuer; the defaults are for development."""
    problems = []
    if settings.SEVERINO_SITE_HOST == "localhost":
        problems.append(
            checks.Warning(
                "The site host resolves to localhost.",
                hint="Set DJANGO_CSRF_TRUSTED_ORIGINS or DJANGO_ALLOWED_HOSTS to the host "
                "this deployment is reached at, or SEVERINO_SITE_HOST to override.",
                id="hq.W111",
            )
        )
    if not settings.OIDC_ISSUER:
        problems.append(
            checks.Warning(
                "SEVERINO_OIDC_ISSUER is unset, so every sign-in is refused.",
                hint="Set it to the identity provider's issuer URL.",
                id="hq.W112",
            )
        )
    return problems


@checks.register(checks.Tags.security, deploy=True)
def static_live_needs_debug(app_configs=None, **kwargs):
    """Serving static files from the source trees is a development mode only."""
    if getattr(settings, "STATIC_LIVE", False) and not settings.DEBUG:
        return [
            checks.Error(
                "STATIC_LIVE is on while DEBUG is off.",
                hint="Unset DJANGO_WHITENOISE_AUTOREFRESH, or turn DEBUG on for local development.",
                id="hq.E110",
            )
        ]
    return []

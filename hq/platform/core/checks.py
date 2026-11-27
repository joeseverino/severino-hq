"""System checks on a deployment's settings, run by ``manage.py check --deploy``,
and on the pages the composed routes serve.

A system check runs before every management command, so nothing here reads the
source tree: a rule about source is an architecture test, which a gate runs
once, and one about a template is a shape refused as it loads
(``hq.platform.core.hand_drawn``).
"""

from django.conf import settings
from django.core import checks

lazy from hq.platform.application.pages import unreached_pages


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
    if settings.STATIC_LIVE and not settings.DEBUG:
        return [
            checks.Error(
                "STATIC_LIVE is on while DEBUG is off.",
                hint="Unset DJANGO_WHITENOISE_AUTOREFRESH, or turn DEBUG on for local development.",
                id="hq.E110",
            )
        ]
    return []


@checks.register(checks.Tags.urls)
def every_page_is_reached(app_configs=None, **kwargs):
    """A page nothing links to is one no reader finds."""
    return [
        checks.Error(
            f"No link reaches the page {name!r}.",
            hint="Give it a NavigationItem in its domain's declaration, or a page_trail naming the page it opens from.",
            id="hq.E120",
        )
        for name in unreached_pages()
    ]

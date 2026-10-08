"""
Severino HQ settings.

Production guidance:
- DEBUG must be False (set DJANGO_DEBUG=0).
- SECRET_KEY must come from the environment.
- ALLOWED_HOSTS must be set explicitly.
- Bind the app to localhost or the Tailscale interface, never the public internet.
- Uploaded media live OUTSIDE the application code (set SEVERINO_MEDIA_ROOT).
"""

import contextlib
import os
import secrets
import shlex
import sys
import tempfile
from importlib import import_module
from pathlib import Path

from django.utils.csp import CSP

from hq.config.devtools import (
    DEBUG_TOOLBAR_APP,
    DEBUG_TOOLBAR_MIDDLEWARE,
    debug_toolbar_enabled,
    without_trusted_types,
)

from .paths import database_path

# The plugin registry is loaded by name: settings is imported by everything, and
# a static import here would put it inside the application's own import cycle.
installed_plugin_apps = import_module("hq.platform.application.plugins").installed_plugin_apps
host_apps = import_module("hq.platform.application.domains").host_apps

BASE_DIR = Path(__file__).resolve().parents[2]

# Production mounts the 1Password-rendered app env (shell-quoted KEY='value'
# lines) at this path. Loading it here (not only in the entrypoint) means
# every process in the container gets it, including `docker compose exec`
# sessions (hq sync / shell / superuser), which never run the entrypoint.
# setdefault: real environment variables always win.
_APP_ENV_FILE = Path(os.environ.get("SEVERINO_APP_ENV_PATH", "/run/secrets/severino_hq_env"))
if _APP_ENV_FILE.is_file():
    for _token in shlex.split(_APP_ENV_FILE.read_text(encoding="utf-8")):
        _key, _sep, _value = _token.partition("=")
        if _sep:
            os.environ.setdefault(_key, _value)


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    """A whole number from the environment, or the default when it is not one.

    Refusing to start over a malformed cadence value would take HQ down to
    protect a polling interval. The default is always a working answer.

    A ``minimum`` marks a value HQ will not guess, such as the month a fiscal
    year starts: not a number, or outside the bounds, refuses to start.
    """

    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = None
    if minimum is None:
        return default if value is None else value
    if value is None or value < minimum or (maximum is not None and value > maximum):
        allowed = f"at least {minimum}" if maximum is None else f"between {minimum} and {maximum}"
        raise RuntimeError(f"{name} must be {allowed}.")
    return value


def env_list(name: str, default: list[str] | None = None) -> list[str]:
    raw = os.environ.get(name, "")
    items = [chunk.strip() for chunk in raw.split(",") if chunk.strip()]
    return items or (default or [])


def env_secret(name: str) -> str:
    """Load a secret from NAME_FILE, falling back to NAME for local use."""

    file_name = os.environ.get(f"{name}_FILE", "").strip()
    value = os.environ.get(name, "")
    if file_name and value:
        raise RuntimeError(f"Set only one of {name} or {name}_FILE.")
    if not file_name:
        return value
    try:
        return Path(file_name).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"Could not read {name}_FILE.") from exc


# ----- Core security -----------------------------------------------------------

DEBUG = env_bool("DJANGO_DEBUG", default=False)

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if DEBUG:
        # Generated per process rather than written down: a constant here
        # would be a signing key in source, and development servers hold real
        # sessions. Sessions do not survive a restart, which is the honest
        # behaviour for a key nobody chose; `scripts/dev.sh` supplies a real
        # one, so this is the floor rather than the usual path.
        SECRET_KEY = secrets.token_urlsafe(64)
    else:
        raise RuntimeError("DJANGO_SECRET_KEY must be set in the environment for production.")

ALLOWED_HOSTS = env_list(
    "DJANGO_ALLOWED_HOSTS",
    default=["localhost", "127.0.0.1"] if DEBUG else [],
)

CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])


def site_host(origins, hosts) -> str:
    """The first trusted origin's host, else the first concrete allowed host."""
    from urllib.parse import urlsplit

    for origin in origins:
        host = urlsplit(origin).hostname or ""
        if host and "*" not in host:
            return host
    for host in hosts:
        if host and not host.startswith(".") and "*" not in host:
            return host
    return "localhost"


# The canonical name of this platform, for anything that leaves it: a printed
# page, an exported file, the host a plain-HTTP request is sent back to. Not
# taken from the request: a brief printed from a laptop names the same host as
# one printed from the server.
SEVERINO_SITE_HOST = os.environ.get("SEVERINO_SITE_HOST") or site_host(CSRF_TRUSTED_ORIGINS, ALLOWED_HOSTS)

# Tighter defaults in production. These can be overridden via env behind a
# TLS-terminating reverse proxy on a Tailscale-only interface.
SESSION_COOKIE_SECURE = env_bool("DJANGO_SESSION_COOKIE_SECURE", default=not DEBUG)
CSRF_COOKIE_SECURE = env_bool("DJANGO_CSRF_COOKIE_SECURE", default=not DEBUG)
SESSION_COOKIE_HTTPONLY = True
# The `__Host-` prefix is a rule the *browser* enforces, and the only cookie
# hardening that survives an attacker who controls a sibling name. Secure,
# HttpOnly and SameSite all describe a cookie HQ set; none of them stop
# something at another host under this domain from setting a cookie of the same
# name that HQ then reads back as its own. A prefixed cookie may only be set
# over HTTPS, for the exact host, at path `/`, with no Domain, so a sibling
# cannot write one at all, and the ambiguity is gone rather than mitigated.
#
# Derived from the Secure flag rather than declared, because a browser silently
# refuses to store a `__Host-` cookie that is not Secure. Hard-coding the name
# would work in production and break every plain-HTTP development session, in
# the way that looks like sign-in is broken rather than like a cookie was
# rejected.
SESSION_COOKIE_NAME = "__Host-sessionid" if SESSION_COOKIE_SECURE else "sessionid"
CSRF_COOKIE_NAME = "__Host-csrftoken" if CSRF_COOKIE_SECURE else "csrftoken"
# Stated rather than inherited. Django's default is already `Lax`, and `Lax` is
# the correct answer here: `Strict` would withhold the session cookie on the
# top-level redirect back from the identity provider, which is sign-in itself.
# Written down so that reasoning is visible to the next person to consider
# tightening it, and so the connection page can report a value HQ chose.
SESSION_COOKIE_SAMESITE = os.environ.get("DJANGO_SESSION_COOKIE_SAMESITE", "Lax")
CSRF_COOKIE_SAMESITE = os.environ.get("DJANGO_CSRF_COOKIE_SAMESITE", "Lax")
# How long a signed-in session lasts. Django's default is two weeks, and HQ
# never spoke to Pocket ID again after `auth.login`: the group allowlist in
# `core.oidc.verify_claims` runs at sign-in and nowhere else. Disabling an
# account or revoking a passkey changed nothing for a fortnight. Absolute
# rather than sliding: a write per request shows up in the flat-query budgets,
# and SessionRefresh below is what re-checks the provider.
SESSION_COOKIE_AGE = env_int("SEVERINO_SESSION_SECONDS", 12 * 60 * 60)
CSRF_COOKIE_HTTPONLY = False  # Django needs JS access for the token header
# A stale form is answered on HQ's own refusal page, in the site's frame.
CSRF_FAILURE_VIEW = "hq.platform.core.error_views.csrf_failure"
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https") if env_bool("DJANGO_BEHIND_TLS_PROXY") else None
# A year, on by default. HQ is HTTPS-only behind the proxy, and the header
# costs nothing until a browser has already reached it over TLS once.
SECURE_HSTS_SECONDS = env_int("DJANGO_HSTS_SECONDS", 31536000)
# Subdomains included by default, because the guarantee is about the name and
# everything under it. Nothing is served under HQ's host, so this costs nothing
# today and forecloses a plain-HTTP sibling appearing there later.
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_HSTS_INCLUDE_SUBDOMAINS", default=True)
# Preload stays opt-in. It is a submission to a list baked into browsers, is
# slow to undo, and is meaningless for a name the public internet cannot
# resolve, which is deliberately true of this deployment.
SECURE_HSTS_PRELOAD = env_bool("DJANGO_HSTS_PRELOAD")

# HQ binds a plain HTTP port, and behind a TLS proxy that port stays reachable
# by anything that can route to the host, so the browser UI has two front
# doors: the proxied HTTPS name, and the raw port with no TLS, no HSTS, and
# none of the proxy's own source restrictions.
#
# Told that a proxy terminates TLS, HQ refuses the second one: a request
# that did not arrive as HTTPS is redirected to the canonical name. The
# healthcheck is exempt because it deliberately probes the raw port from inside
# the container's own network namespace, which is the one caller for whom plain
# HTTP is the correct request.
#
# Keyed on the proxy flag rather than on `not DEBUG`: a deployment that
# terminates TLS itself, or genuinely serves plain HTTP on a private segment,
# should not be redirected to a scheme nothing is listening on.
SECURE_SSL_REDIRECT = env_bool("DJANGO_BEHIND_TLS_PROXY")
SECURE_SSL_HOST = SEVERINO_SITE_HOST if SECURE_SSL_REDIRECT else None
SECURE_REDIRECT_EXEMPT = [r"^health/"]

# W008 fires only where the redirect is genuinely off: a deployment with no
# TLS proxy in front of it, which is development. Silenced conditionally rather
# than always, so `check --deploy --fail-level WARNING` still has an opinion
# about the production posture instead of being told not to look.
SILENCED_SYSTEM_CHECKS = [] if SECURE_SSL_REDIRECT else ["security.W008"]

# Where the browser sends a policy violation. One path, named once, because the
# policy references it and the URLconf has to route it.
SEVERINO_CSP_REPORT_PATH = "/csp-report/"
# What happens when a request reaches for a network, a process or a timer
# (hq.platform.core.outbound): "refuse" raises before the call leaves; "report"
# logs it as outbound.in_request and lets it go, for a composition whose
# extensions have not yet moved such work to a job.
SEVERINO_OUTBOUND_IN_REQUEST = os.environ.get("SEVERINO_OUTBOUND_IN_REQUEST", "refuse")

# Django owns the browser security boundary. Scripts are limited to same-origin
# assets or per-response nonces; objects and framing are disabled outright.
# Inline styles remain allowed so a chart can place a mark with a per-datum
# custom property; application templates keep styles in the static bundle.
#
# `require-trusted-types-for` is the one directive here that is not about where
# content may come from. Every other line describes an origin; this one removes
# a class of bug. With it, assigning a string to `innerHTML`, `outerHTML`,
# `srcdoc` or a script URL (or handing one to `DOMParser`) throws instead of
# parsing, so a DOM-based cross-site scripting sink cannot execute even if one
# is introduced.
#
# `trusted-types` names exactly one policy and does not permit duplicates. HQ's
# progressive enhancement is server-rendered HTML swapped in, so one place does
# have to turn a response body into markup; `hq-fragment` in `static/js/fragment.js`
# is that place and the only one. Because the name is taken and cannot be
# claimed twice, script that gets onto the page cannot create a policy of its
# own to reach a sink with, which is the property that makes a single
# audited sink worth more than a blanket ban nobody could satisfy.
SECURE_CSP = {
    "default-src": [CSP.SELF],
    "script-src": [CSP.SELF, CSP.NONCE],
    "style-src": [CSP.SELF, CSP.UNSAFE_INLINE],
    "img-src": [CSP.SELF, "data:"],
    "font-src": [CSP.SELF],
    "connect-src": [CSP.SELF],
    "object-src": [CSP.NONE],
    "base-uri": [CSP.SELF],
    "form-action": [CSP.SELF],
    "frame-ancestors": [CSP.NONE],
    "require-trusted-types-for": ["'script'"],
    "trusted-types": ["hq-fragment"],
    # Both spellings. `report-to` is the current one and needs the
    # Reporting-Endpoints header that `core.middleware` sends; `report-uri` is
    # deprecated and is what most browsers still act on. A policy nobody
    # reports on is a policy nobody knows is being probed.
    "report-to": ["csp"],
    "report-uri": [SEVERINO_CSP_REPORT_PATH],
}

# The API reference at /api/docs/ is the one page served without the Trusted
# Types directives: the vendored Scalar bundle (static/vendor/scalar) writes
# strings into innerHTML through Vue and its markdown renderer, and no
# configuration of it creates only the `hq-fragment` policy. Spelled as a
# derivation rather than a second literal policy, so tightening the real one
# cannot leave a stale copy behind. Every source stays 'self'; it needs no
# inline script and, configured jitless, no eval.
SEVERINO_API_REFERENCE_CSP = without_trusted_types(SECURE_CSP)

# ----- Who may reach HQ at all ------------------------------------------------

# HQ answers the private LAN, the tailnet, and loopback (the container
# healthcheck). Defaults are in `core.network`; both lists are overridable for
# a deployment whose network does not look like this one.
SEVERINO_ENFORCE_TRUSTED_NETWORK = env_bool("SEVERINO_ENFORCE_TRUSTED_NETWORK", default=True)
# The tailnet and loopback, and nothing else.
#
# Not the private LAN ranges: a home LAN is not a trust boundary. It holds a
# television, a printer, whatever a guest joined.
#
# The tailnet is the boundary this deployment actually maintains (every peer
# on it is an enrolled device with a key and a policy) so it is the one HQ
# states. Loopback stays because the container healthcheck probes it from
# inside its own network namespace, and a reverse proxy on the same host
# reaches HQ there.
#
# Spelled out as the default rather than left to configuration, so a deployment
# that sets nothing is closed to everything but its VPN. A deployment whose
# network genuinely is the boundary adds its ranges explicitly, so opening
# one is a decision rather than a default.
SEVERINO_TRUSTED_NETWORKS = env_list(
    "SEVERINO_TRUSTED_NETWORKS",
    default=[
        "127.0.0.0/8",
        "::1/128",
        "100.64.0.0/10",  # Tailscale (CGNAT)
        "fd7a:115c:a1e0::/48",  # Tailscale (IPv6 ULA)
    ],
)
# Whose `X-Forwarded-For` HQ believes. Narrower than the networks above on
# purpose: this is not "who may connect", it is "who may *name someone else*",
# which is a far stronger claim to accept. The TLS proxy is on the LAN; a
# tailnet peer is a client, not infrastructure, and must not be able to
# nominate the address HQ judges it by.
#
# Loopback only by default, because the paragraph above is the rule and
# trusting the private ranges would be an exception that swallows it. HQ binds the host's
# network namespace, so any peer on the LAN or tailnet can reach the port
# directly and, if trusted, name whatever address it likes. That address is
# written into the audit log as the source of a failed sign-in and read back
# out by the throttle, so trusting a range corrupts the evidence and the gate
# that reads it, together and invisibly.
#
# A deployment behind a proxy names that proxy explicitly; see .env.example.
SEVERINO_TRUSTED_PROXIES = env_list(
    "SEVERINO_TRUSTED_PROXIES",
    default=["127.0.0.0/8", "::1/128"],
)

# Sign-in throttling for the break-glass password path. Read back out of the
# audit log by `core.throttle`; see that module for why there is no counter.
SEVERINO_LOGIN_MAX_ATTEMPTS = env_int("SEVERINO_LOGIN_MAX_ATTEMPTS", 5)
SEVERINO_LOGIN_WINDOW_SECONDS = env_int("SEVERINO_LOGIN_WINDOW_SECONDS", 900)

# ----- Apps --------------------------------------------------------------------

INSTALLED_APPS = [
    # No ``django.contrib.admin``: every write goes through the capability
    # policy, and the admin is a write path that policy does not see.
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "mozilla_django_oidc",
    # Severino HQ: the machinery, then every app a host domain declares in
    # application/domains.py, then the extensions.
    "hq.platform.core",
    *host_apps(),
    "hq.platform.search_index",
    "hq.platform.api",
    *installed_plugin_apps(),
]

MIDDLEWARE = [
    # Before everything. An address that may not talk to HQ should not reach
    # the session store, the login form, or the audit log.
    "hq.platform.core.network.TrustedNetworkMiddleware",
    "hq.platform.core.middleware.RequestContextMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # After authentication, because the flag is read off the signed-in
    # operator's session, and around the view rather than the template, because
    # every number is decided before a template sees it.
    "hq.platform.core.middleware.DemoModeMiddleware",
    "hq.platform.core.middleware.LoginRequiredMiddleware",
    "hq.platform.core.middleware.CurrentUserMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "hq.platform.core.middleware.ProjectionMiddleware",
]

ROOT_URLCONF = "hq.config.urls"

# Whether a rendered template is reused rather than re-read from disk. Left
# implicit, Django always wraps the loaders in the cached loader, whatever DEBUG
# says, and only runserver's autoreloader clears it; under uvicorn an edited
# template would not show until a restart. Explicit loaders make caching a
# switch of its own, on by default only with DEBUG off.
TEMPLATE_CACHE = env_bool("DJANGO_TEMPLATE_CACHE", default=not DEBUG)

_TEMPLATE_LOADERS = [
    "django.template.loaders.filesystem.Loader",
    "django.template.loaders.app_directories.Loader",
]

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        # Loaders are listed explicitly, so APP_DIRS must be off: Django refuses
        # a configuration that sets both, since app_directories.Loader above is
        # what APP_DIRS is shorthand for.
        "APP_DIRS": False,
        "OPTIONS": {
            "loaders": (
                [("django.template.loaders.cached.Loader", _TEMPLATE_LOADERS)] if TEMPLATE_CACHE else _TEMPLATE_LOADERS
            ),
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.csp",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "hq.platform.core.context_processors.site",
                "hq.platform.core.context_processors.nav",
                "hq.platform.core.context_processors.auth_config",
                "hq.platform.core.context_processors.connection",
                "hq.platform.core.context_processors.agent_access",
                "hq.platform.core.context_processors.appearance",
            ],
            "builtins": [
                "hq.platform.core.templatetags.value_tags",
                "hq.platform.core.templatetags.action_tags",
                "hq.platform.core.templatetags.table_tags",
            ],
        },
    },
]

WSGI_APPLICATION = "hq.config.wsgi.application"


# ----- Caches ------------------------------------------------------------------

CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    # What the estate pages derive, keyed by the revisions of the tables each
    # fact reads (hq.platform.application.derivations). In the database, so
    # every process sees one copy and a value stored inside a transaction
    # commits or rolls back with the rows and revisions it was derived from.
    "derived": {
        "BACKEND": "django.core.cache.backends.db.DatabaseCache",
        "LOCATION": "hq_derived",
        "TIMEOUT": 24 * 60 * 60,
        # A fact is stored once per revision it was derived at, and an old
        # revision is never asked for again: a small table, culled by halves.
        "OPTIONS": {"MAX_ENTRIES": 120, "CULL_FREQUENCY": 2},
    },
}

# ----- Database ----------------------------------------------------------------

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(database_path(BASE_DIR, os.environ.get("SEVERINO_DATABASE_PATH"))),
        "OPTIONS": {
            "init_command": ("PRAGMA journal_mode=WAL;PRAGMA synchronous=NORMAL;PRAGMA foreign_keys=ON;"),
            "transaction_mode": "IMMEDIATE",
            # How long a writer waits for another writer before giving up.
            # WAL lets readers carry on through a write, but writers are still
            # one at a time, and a job importing an archive holds the write
            # lock in bursts while somebody is browsing the site. The default
            # is five seconds and then a 500 on an unrelated page; waiting is
            # the correct behaviour, since the other writer is about to finish.
            "timeout": 30,
        },
        "TEST": {
            # A file, not the in-memory database Django would otherwise use.
            # In-memory SQLite shares connections through a cache whose
            # locking is not WAL's, so a background job writing during a test
            # fails with "database table is locked": an error production
            # cannot produce. A file gives the suite the same journal mode,
            # lock and timeout as the running host, for a couple of seconds.
            #
            # In the temporary directory rather than beside the real database:
            # `data/` is a mounted volume in production and does not exist in
            # the composed image, where the suite runs as its own admission
            # gate. Only being a file matters, not where the file is.
            #
            # Named for the process, because two suites on one machine would
            # otherwise share the file: `mise run check` runs its suites
            # concurrently, and running it alongside `mise run ci` is the
            # normal way to check work. A shared file makes them fail each other
            # with a locked table: a failure that says nothing about the change
            # and costs a while to attribute. Django's parallel workers suffix
            # this name per worker, so they stay distinct within a run too.
            "NAME": os.environ.get(
                "SEVERINO_TEST_DATABASE_PATH",
                str(Path(tempfile.gettempdir()) / f"severino-test-{os.getpid()}.sqlite3"),
            ),
        },
    }
}

# Django's own runner, plus a WAL checkpoint before it clones the test database
# for parallel workers. See core/test_runner.py for why that is necessary.
TEST_RUNNER = "hq.platform.core.test_runner.SeverinoTestRunner"

# Under test, a warning from this codebase and any leaked resource fail the run.
# Here rather than in the runner because settings are what every parallel
# worker imports on start. See config/warning_policy.py.
RUNNING_TESTS = sys.argv[1:2] == ["test"]
# Whether a list read raises on a relation or deferred field it did not fetch
# (see ``application.projection.guarded``). On where a developer sees it.
SEVERINO_STRICT_FETCH = DEBUG or RUNNING_TESTS
if RUNNING_TESTS:
    from hq.config.warning_policy import enforce as _enforce_warning_policy

    _enforce_warning_policy(BASE_DIR)

    # Tests upload receipts and write exports as real files. Without this they
    # landed in var/ inside the working tree. One directory each per run, made by
    # the process that starts it and inherited by parallel workers through the
    # environment, removed when the run ends. An explicit setting still wins.
    import atexit as _atexit
    import shutil as _shutil

    for _variable, _kind in (
        ("SEVERINO_MEDIA_ROOT", "media"),
        ("SEVERINO_EXPORTS_ROOT", "exports"),
    ):
        if _variable not in os.environ:
            os.environ[_variable] = tempfile.mkdtemp(prefix=f"severino-test-{_kind}-")
            _atexit.register(_shutil.rmtree, os.environ[_variable], True)

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ----- Auth --------------------------------------------------------------------

# Argon2 hashes every new password. The rest verify a stored hash in an older
# format, and Django re-hashes it with Argon2 at the next successful sign-in.
PASSWORD_HASHERS = [
    "hq.platform.core.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
# The marker matters. Without it, signing out under SSO-only redirects
# straight back into a still-valid Pocket ID session and signs the operator
# back in: a sign-out button that visibly does nothing.
LOGOUT_REDIRECT_URL = "/accounts/login/?signed_out=1"

# The commit this build is, and the repository it came from: stamped into the
# image by CI (and set from the checkout by the dev stack). Empty for a build
# that did not say, which HQ reports as unknown rather than guessing.
SEVERINO_HQ_SOURCE = os.environ.get("SEVERINO_HQ_SOURCE", "").rstrip("/")
SEVERINO_HQ_REVISION = os.environ.get("SEVERINO_HQ_REVISION", "")

# Pocket ID / OIDC SSO is how a person signs in.
SEVERINO_OIDC_ENABLED = env_bool("SEVERINO_OIDC_ENABLED")

# Re-check a live session against Pocket ID once its ID token ages out, which
# is what makes revoking somebody there take effect here. Inserted rather than
# declared inline because the flag is read below the middleware list.
if SEVERINO_OIDC_ENABLED:
    MIDDLEWARE.insert(
        MIDDLEWARE.index("django.contrib.auth.middleware.AuthenticationMiddleware") + 1,
        "hq.platform.core.oidc.HQSessionRefresh",
    )
OIDC_RENEW_ID_TOKEN_EXPIRY_SECONDS = env_int("SEVERINO_OIDC_RENEW_SECONDS", 15 * 60)

# The password form exists only where SSO does not.
#
# Derived rather than configured, because the two set independently is how a
# deployment ends up with single sign-on and a password door open beside it.
#
# With this off there is no password to guess, so brute force and credential
# stuffing stop being reachable rather than being rate-limited. Pocket ID holds
# the only credential, where the passkey, the MFA policy and revocation already
# live.
#
# The override is the break-glass path for the day SSO itself is what is
# broken: set it, restart, and the form is back. Deliberate, and it lands in
# the audit log the moment it is used.
SEVERINO_PASSWORD_LOGIN_ENABLED = env_bool("SEVERINO_PASSWORD_LOGIN_ENABLED", default=not SEVERINO_OIDC_ENABLED)

# The backend is removed, not merely unused: the guarantee has to hold for
# any caller of `authenticate()`, not only for the login view.
AUTHENTICATION_BACKENDS = ["hq.platform.core.oidc.HQOIDCAuthenticationBackend"] + (
    ["django.contrib.auth.backends.ModelBackend"] if SEVERINO_PASSWORD_LOGIN_ENABLED else []
)

SEVERINO_OIDC_ALLOWED_EMAILS = {email.lower() for email in env_list("SEVERINO_OIDC_ALLOWED_EMAILS")}
SEVERINO_OIDC_ALLOWED_GROUPS = set(env_list("SEVERINO_OIDC_ALLOWED_GROUPS"))

# Empty rejects every token: no issuer matches it.
OIDC_ISSUER = os.environ.get("SEVERINO_OIDC_ISSUER", "").rstrip("/")
OIDC_RP_CLIENT_ID = os.environ.get("SEVERINO_OIDC_CLIENT_ID", "")
OIDC_RP_CLIENT_SECRET = os.environ.get("SEVERINO_OIDC_CLIENT_SECRET", "")
# The `email` scope is requested only when an email allowlist is configured.
# Without it the claim never arrives, so SEVERINO_OIDC_ALLOWED_EMAILS could be
# set and simply never match: failing closed, but silently.
OIDC_RP_SCOPES = "openid profile groups" + (" email" if SEVERINO_OIDC_ALLOWED_EMAILS else "")
OIDC_RP_SIGN_ALGO = "RS256"
OIDC_OP_AUTHORIZATION_ENDPOINT = f"{OIDC_ISSUER}/authorize"
OIDC_OP_TOKEN_ENDPOINT = f"{OIDC_ISSUER}/api/oidc/token"
OIDC_OP_USER_ENDPOINT = f"{OIDC_ISSUER}/api/oidc/userinfo"
OIDC_OP_JWKS_ENDPOINT = f"{OIDC_ISSUER}/.well-known/jwks.json"
OIDC_CREATE_USER = env_bool("SEVERINO_OIDC_CREATE_USER", default=True)
# Where a sign-in that did not finish lands: the login page, told to stop and
# say why rather than going straight back to the provider in a loop.
LOGIN_REDIRECT_URL_FAILURE = "/accounts/login/?sso_failed=1"
OIDC_USE_PKCE = True
# Signing in waits on the provider, and nothing waits without a bound: every
# call the exchange makes (token, keys, userinfo) gives up after this long.
OIDC_TIMEOUT = env_int("SEVERINO_OIDC_TIMEOUT_SECONDS", 10)
OIDC_STORE_ACCESS_TOKEN = False
OIDC_STORE_ID_TOKEN = False
OIDC_AUTHENTICATION_CALLBACK_URL = "oidc_authentication_callback"

# Machine-client API. HQ verifies access tokens Pocket ID issued for this
# resource and mints no credential of its own, so there is nothing to revoke
# here: revocation is done on the client in Pocket ID.
#
# Empty disables the surface fail-closed, and must: without a resource to check
# `aud` against, a token minted for any other API on the same issuer would
# verify here on signature alone.
SEVERINO_API_RESOURCE = os.environ.get("SEVERINO_API_RESOURCE", "")
# Clock skew allowance between the phone, Pocket ID and HQ. Small on purpose:
# the tokens are short-lived, and a generous window is a longer replay window.
SEVERINO_API_LEEWAY_SECONDS = env_int("SEVERINO_API_LEEWAY_SECONDS", 30)
# A retry key represents one machine request for this long. The record is
# durable because a process restart is exactly when an in-memory replay cache
# would fail the client that needs it.
SEVERINO_API_IDEMPOTENCY_TTL_SECONDS = env_int("SEVERINO_API_IDEMPOTENCY_TTL_SECONDS", 86400, minimum=60)

# Encrypts the few secrets an operator deliberately hands to HQ: today, the
# private key of an internally signed certificate that has to reach a proxy.
# Unset, HQ refuses to hold one rather than storing it in the clear; see
# core.secrets. Not a provider credential: those stay outside the web container.
SEVERINO_SECRET_STORE_KEY = env_secret("SEVERINO_SECRET_STORE_KEY")

# Private MCP endpoint, enforced by the ASGI boundary. Callers authenticate with
# an access token from the identity provider (SEVERINO_API_RESOURCE and the
# issuer settings); without that, or with no allowed hosts, MCP is off.
SEVERINO_MCP_ALLOWED_HOSTS = env_list("SEVERINO_MCP_ALLOWED_HOSTS")
SEVERINO_MCP_ALLOWED_NETWORKS = env_list(
    "SEVERINO_MCP_ALLOWED_NETWORKS",
    default=["100.64.0.0/10", "fd7a:115c:a1e0::/48"],
)
SEVERINO_MCP_ALLOWED_ORIGINS = env_list("SEVERINO_MCP_ALLOWED_ORIGINS")
# Whether HQ may queue a repair for a finding on its own. Off by default: the
# first release of anything that acts unattended should be watched proposing
# before it is trusted acting. Even on, it only ever queues: the controller
# still pulls and claims, so no provider credential nears the web process.
SEVERINO_FINDINGS_AUTO_REMEDY = env_bool("SEVERINO_FINDINGS_AUTO_REMEDY", False)
# What an agent may do beyond reading, one switch each, all off by default: a
# deployment decides each on its own. Why each stands apart is beside its grant
# in application.security.mcp_principal.
SEVERINO_MCP_ENABLE_WRITES = env_bool("SEVERINO_MCP_ENABLE_WRITES", False)
SEVERINO_MCP_ENABLE_DOC_SYNC = env_bool("SEVERINO_MCP_ENABLE_DOC_SYNC", False)
SEVERINO_MCP_ENABLE_PRUNE = env_bool("SEVERINO_MCP_ENABLE_PRUNE", False)
SEVERINO_MCP_ENABLE_DELETES = env_bool("SEVERINO_MCP_ENABLE_DELETES", False)
SEVERINO_MCP_ENABLE_INFRASTRUCTURE = env_bool("SEVERINO_MCP_ENABLE_INFRASTRUCTURE", False)
SEVERINO_MCP_ENABLE_CERT_RENEWAL = env_bool("SEVERINO_MCP_ENABLE_CERT_RENEWAL", False)
SEVERINO_MCP_ENABLE_LOOKUP = env_bool("SEVERINO_MCP_ENABLE_LOOKUP", False)
SEVERINO_MCP_ENABLE_CALENDAR = env_bool("SEVERINO_MCP_ENABLE_CALENDAR", False)
# How long a change held for a person's approval stands before it lapses. A day:
# the person it waits for sleeps, and a request still clickable a month later is
# a change nobody is looking at any more being applied on an old decision.
SEVERINO_APPROVAL_WINDOW_HOURS = env_int("SEVERINO_APPROVAL_WINDOW_HOURS", 24)
SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS = env_bool("SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS", False)


# ----- Controller cadence ------------------------------------------------------
#
# Applying queued work and sweeping what the providers hold run on different
# clocks. The first happens when the doorbell rings; the second is due when HQ
# says it is, and HQ says so less often when nobody is looking. See
# `application/cadence.py`.

# How stale the estate may look while somebody is using HQ. This is the number
# that decides whether the board is worth trusting at a glance.
#
# Longer than a sweep takes, or the controller never rests: one sweep is due
# again before the last has finished, every run is a sweep, and a doorbell rung
# while it runs is not heard until it ends. The controller being idle between
# sweeps is what lets queued work start the moment it is asked for.
SEVERINO_SWEEP_INTERVAL_ACTIVE_SECONDS = env_int("SEVERINO_SWEEP_INTERVAL_ACTIVE_SECONDS", 300)
# And while nobody is. Every sweep costs a call to each provider, and nothing
# reads the answer until somebody opens a page.
SEVERINO_SWEEP_INTERVAL_IDLE_SECONDS = env_int("SEVERINO_SWEEP_INTERVAL_IDLE_SECONDS", 12 * 60 * 60)
# How long after a request HQ still counts as in use. Long enough to cover
# reading a page and acting on it without the tab being open throughout.
SEVERINO_ACTIVE_WINDOW_SECONDS = env_int("SEVERINO_ACTIVE_WINDOW_SECONDS", 900)
# Days routine machine audit events (core.audit.ROUTINE_EVENTS) are kept.
# Everything else in the audit log is kept indefinitely.
SEVERINO_AUDIT_ROUTINE_DAYS = env_int("SEVERINO_AUDIT_ROUTINE_DAYS", 30)
# How often the in-use marker is rewritten. Every request checks it; only the
# first in each interval writes.
SEVERINO_ACTIVITY_THROTTLE_SECONDS = env_int("SEVERINO_ACTIVITY_THROTTLE_SECONDS", 60)
# How often each source's arrival is written to HQ's request-path reading, per
# process. Every request is counted in memory; 0 records nothing.
SEVERINO_REQUEST_PATH_SECONDS = env_int("SEVERINO_REQUEST_PATH_SECONDS", 300)
# Where HQ leaves each marker. The doorbell has to be somewhere the host can
# watch; the in-use marker is read only by HQ and defaults beside the database.
SEVERINO_CONTROLLER_DOORBELL = os.environ.get("SEVERINO_CONTROLLER_DOORBELL", "")
SEVERINO_ACTIVITY_MARKER = os.environ.get("SEVERINO_ACTIVITY_MARKER", "")
# The Unix socket the controller bridge is served on, in a directory only this
# account can enter. Unset, the process serves no bridge; set, it serves one or
# does not start.
SEVERINO_BRIDGE_SOCKET = os.environ.get("SEVERINO_BRIDGE_SOCKET", "")

# Extra dashboard links, as a JSON list of {label, sub, href}. One deployment's
# status page is a fact about that deployment; the consoles HQ can reach are
# derived from the connections a controller reported and need no entry here.
SEVERINO_DASHBOARD_LINKS = os.environ.get("SEVERINO_DASHBOARD_LINKS", "")
# ----- I18N --------------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("DJANGO_TIME_ZONE", "America/Chicago")
USE_I18N = True
USE_TZ = True

# Dates and times as the operator reads them, 5/23/26 5:49 PM, in one module.
FORMAT_MODULE_PATH = ["hq.config.formats"]


# ----- Static & media ----------------------------------------------------------

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = Path(os.environ.get("DJANGO_STATIC_ROOT", str(BASE_DIR / "var" / "static")))

# /static/ has one server, the ASGI mount in hq.config.asgi (core.static), and
# one collected tree: the image build runs collectstatic, so STATIC_ROOT is
# part of the image, read-only, and a container start collects nothing.
#
# STATIC_LIVE serves from the source trees instead, uncached, so an edited
# stylesheet shows on the next reload. A deployment check (hq.E110) refuses it
# with DEBUG off; a local dev server may run it with DEBUG off. Off by default:
# a served request should not search the filesystem.
STATIC_LIVE = env_bool("DJANGO_WHITENOISE_AUTOREFRESH", default=DEBUG)

# Collected assets are named by their content (css/app.3f2a1b9c0d4e.css), so
# core.static can cache them forever, and each has a gzip copy beside it, so
# nothing is compressed while serving. Live serving keeps plain names: the
# source trees it reads have no hashed copies, and it sends no-cache anyway.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        if STATIC_LIVE
        else "hq.platform.core.static.HashedStaticStorage",
        # Public by nature and collected by root into the image, so readable
        # by the account that serves them: not the private mode uploads get.
        "OPTIONS": {"file_permissions_mode": 0o644, "directory_permissions_mode": 0o755},
    },
}

# Media (uploaded receipts) lives OUTSIDE the app code in production.
# Receipt files are served only through an auth-protected view, never via MEDIA_URL.
MEDIA_ROOT = Path(os.environ.get("SEVERINO_MEDIA_ROOT", str(BASE_DIR / "var" / "media")))
MEDIA_URL = "/_internal-media/"  # not actually exposed; receipts use protected view

EXPORTS_ROOT = Path(os.environ.get("SEVERINO_EXPORTS_ROOT", str(BASE_DIR / "var" / "exports")))

# Upload guardrails.
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024  # 10 MB
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
FILE_UPLOAD_PERMISSIONS = 0o640


# ----- Logging -----------------------------------------------------------------

SEVERINO_LOG_LEVEL = os.environ.get("SEVERINO_LOG_LEVEL", "INFO").upper()
if SEVERINO_LOG_LEVEL not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
    raise RuntimeError("SEVERINO_LOG_LEVEL must be a standard Python log level.")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {"()": "hq.platform.core.logging.JsonFormatter"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
        },
    },
    "root": {"handlers": ["console"], "level": SEVERINO_LOG_LEVEL},
    "loggers": {
        "django.request": {"level": SEVERINO_LOG_LEVEL, "propagate": True},
        "severino.request": {"level": SEVERINO_LOG_LEVEL, "propagate": True},
        "severino": {"level": SEVERINO_LOG_LEVEL, "propagate": True},
    },
}


# ----- App-specific ------------------------------------------------------------

SEVERINO_SITE_NAME = os.environ.get("SEVERINO_SITE_NAME", "Severino HQ")
SEVERINO_FISCAL_YEAR_START_MONTH = env_int("SEVERINO_FISCAL_YEAR_START_MONTH", 1, minimum=1, maximum=12)
SEVERINO_DOC_REVIEW_INTERVAL_DAYS = env_int("SEVERINO_DOC_REVIEW_INTERVAL_DAYS", 180, minimum=1)

# Cloudflare D1: the contact-form submissions live in a Cloudflare D1
# database, not HQ's SQLite. The contacts app reads/writes it
# over the D1 HTTP API.
# Public DNS and reverse-DNS lookups. The one question HQ cannot answer from
# the inside: a resolver on this network follows the internal rewrites and
# reports the opposite of what the public internet sees.
#
# Unauthenticated by design: no credential of HQ's travels with a lookup,
# which is what makes it acceptable for the web process to make the call
# rather than routing it through the controller. Blanking the endpoint
# disables every lookup surface fail-closed.
SEVERINO_LOOKUP_ENDPOINT = os.environ.get("SEVERINO_LOOKUP_ENDPOINT", "https://dns-lookup.com")
# Short on purpose. This runs in the request path of a page an operator is
# waiting on, and a lookup that has not answered in a few seconds is one the
# page should report as unavailable rather than keep waiting for.
SEVERINO_LOOKUP_TIMEOUT_SECONDS = env_int("SEVERINO_LOOKUP_TIMEOUT_SECONDS", 6)
# Who holds an address, read from the registries that allocated it rather than
# from anyone's copy of them. `rdap.org` is the bootstrap service: it redirects
# to whichever regional registry actually holds the block.
SEVERINO_RDAP_ENDPOINT = os.environ.get("SEVERINO_RDAP_ENDPOINT", "https://rdap.org")

CLOUDFLARE_D1_DATABASE_NAME = os.environ.get("CLOUDFLARE_D1_DATABASE_NAME", "")
CLOUDFLARE_API_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")


# Ensure the directories HQ depends on exist at startup.
for _d in (
    Path(DATABASES["default"]["NAME"]).parent,
    MEDIA_ROOT,
    EXPORTS_ROOT,
    STATIC_ROOT,
):
    # Don't crash at import time on a read-only filesystem; the user will see
    # a clear error from Django when the resource is actually accessed.
    with contextlib.suppress(OSError):
        _d.mkdir(parents=True, exist_ok=True)

# ----- Content index (the public site's published-writeups pull) --------------
# HQ reflects what is live on the public site, mirroring the GitHub refresh:
# fetch an already-public JSON index over HTTP, gated by a Cloudflare Access
# service token. See content/content_sync.py.
# Unset turns the sync off. The project is the one whose public URL serves the
# index, unless named here.
CONTENT_INDEX_URL = os.environ.get("CONTENT_INDEX_URL", "")
CONTENT_INDEX_PROJECT_SLUG = os.environ.get("CONTENT_INDEX_PROJECT_SLUG", "")
CF_ACCESS_CLIENT_ID = env_secret("CF_ACCESS_CLIENT_ID")
CF_ACCESS_CLIENT_SECRET = env_secret("CF_ACCESS_CLIENT_SECRET")


# ----- Development only: Django Debug Toolbar ---------------------------------

# See config/devtools.py. Installed from the dev dependency group, which the image
# never installs; enabled only with DEBUG on and the flag set, outside the suite.
SEVERINO_DEBUG_TOOLBAR = debug_toolbar_enabled(
    debug=DEBUG, requested=env_bool("SEVERINO_DEBUG_TOOLBAR"), testing=RUNNING_TESTS
)
if SEVERINO_DEBUG_TOOLBAR:
    INSTALLED_APPS.append(DEBUG_TOOLBAR_APP)
    # Inside the policy middleware, so the nonce on the toolbar's scripts is the
    # one the header states.
    MIDDLEWARE.insert(
        MIDDLEWARE.index("django.middleware.csp.ContentSecurityPolicyMiddleware") + 1,
        DEBUG_TOOLBAR_MIDDLEWARE,
    )
    SECURE_CSP = without_trusted_types(SECURE_CSP)
# Who sees the toolbar: loopback, unless a developer behind a proxy names more.
# Empty, Django's default, whenever it is off.
INTERNAL_IPS = env_list("SEVERINO_DEBUG_TOOLBAR_IPS", ["127.0.0.1", "::1"]) if SEVERINO_DEBUG_TOOLBAR else []

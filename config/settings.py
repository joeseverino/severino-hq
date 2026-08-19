"""
Severino HQ settings.

Production guidance:
- DEBUG must be False (set DJANGO_DEBUG=0).
- SECRET_KEY must come from the environment.
- ALLOWED_HOSTS must be set explicitly.
- Bind the app to localhost or the Tailscale interface, never the public internet.
- Uploaded media live OUTSIDE the application code (set SEVERINO_MEDIA_ROOT).
"""

from __future__ import annotations

import os
import shlex
import tempfile
from pathlib import Path

from django.utils.csp import CSP

from application.plugins import installed_plugin_apps

BASE_DIR = Path(__file__).resolve().parent.parent

# Production mounts the 1Password-rendered app env (shell-quoted KEY='value'
# lines) at this path. Loading it here — not only in the entrypoint — means
# every process in the container gets it, including `docker compose exec`
# sessions (hq sync / shell / superuser), which never run the entrypoint.
# setdefault: real environment variables always win.
_APP_ENV_FILE = Path(
    os.environ.get("SEVERINO_APP_ENV_PATH", "/run/secrets/severino_hq_env")
)
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
        SECRET_KEY = "dev-insecure-key-do-not-use-in-prod"  # noqa: S105
    else:
        raise RuntimeError(
            "DJANGO_SECRET_KEY must be set in the environment for production."
        )

ALLOWED_HOSTS = env_list(
    "DJANGO_ALLOWED_HOSTS",
    default=["localhost", "127.0.0.1"] if DEBUG else [],
)

CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])

# Tighter defaults in production. These can be overridden via env if you're
# behind a TLS-terminating reverse proxy on a Tailscale-only interface.
SESSION_COOKIE_SECURE = env_bool("DJANGO_SESSION_COOKIE_SECURE", default=not DEBUG)
CSRF_COOKIE_SECURE = env_bool("DJANGO_CSRF_COOKIE_SECURE", default=not DEBUG)
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = False  # Django needs JS access for the token header
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"
SECURE_PROXY_SSL_HEADER = (
    ("HTTP_X_FORWARDED_PROTO", "https") if env_bool("DJANGO_BEHIND_TLS_PROXY") else None
)
# A year, on by default. HQ is HTTPS-only behind the proxy, and the header
# costs nothing until a browser has already reached it over TLS once.
SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_HSTS_INCLUDE_SUBDOMAINS")
SECURE_HSTS_PRELOAD = env_bool("DJANGO_HSTS_PRELOAD")

# SECURE_SSL_REDIRECT is deliberately left unset (Django would warn W008). The
# TLS-terminating reverse proxy (NPM/Caddy) handles http->https; a Django-level
# redirect would also break the container healthcheck, which probes
# http://127.0.0.1:8000 inside the network namespace. The decision is encoded
# here so `check --deploy --fail-level WARNING` can be a hard CI gate.
SILENCED_SYSTEM_CHECKS = ["security.W008"]

# Django owns the browser security boundary. Scripts are limited to same-origin
# assets or per-response nonces; objects and framing are disabled outright.
# Inline styles remain allowed for Django admin compatibility, while application
# templates keep styles in the static bundle.
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
}

# ----- Who may reach HQ at all ------------------------------------------------

# HQ answers the private LAN, the tailnet, and loopback (the container
# healthcheck). Defaults are in `core.network`; both lists are overridable for
# a deployment whose network does not look like this one.
SEVERINO_ENFORCE_TRUSTED_NETWORK = env_bool(
    "SEVERINO_ENFORCE_TRUSTED_NETWORK", default=True
)
# Tailscale hands out addresses from the carrier-grade NAT range; RFC 1918 and
# loopback cover the LAN, Docker's bridge networks and the healthcheck. Spelled
# out as the default rather than left to configuration, so a deployment that
# sets nothing is still closed to the public internet.
SEVERINO_TRUSTED_NETWORKS = env_list(
    "SEVERINO_TRUSTED_NETWORKS",
    default=[
        "127.0.0.0/8",
        "::1/128",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",  # Tailscale (CGNAT)
        "fd7a:115c:a1e0::/48",  # Tailscale (IPv6 ULA)
        "fc00::/7",  # unique local addresses
    ],
)
# Whose `X-Forwarded-For` HQ believes. Narrower than the networks above on
# purpose: this is not "who may connect", it is "who may *name someone else*",
# which is a far stronger claim to accept. The TLS proxy is on the LAN; a
# tailnet peer is a client, not infrastructure, and must not be able to
# nominate the address HQ judges it by.
SEVERINO_TRUSTED_PROXIES = env_list(
    "SEVERINO_TRUSTED_PROXIES",
    default=["127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"],
)

# Sign-in throttling for the break-glass password path. Read back out of the
# audit log by `core.throttle`; see that module for why there is no counter.
SEVERINO_LOGIN_MAX_ATTEMPTS = int(os.environ.get("SEVERINO_LOGIN_MAX_ATTEMPTS", "5"))
SEVERINO_LOGIN_WINDOW_SECONDS = int(
    os.environ.get("SEVERINO_LOGIN_WINDOW_SECONDS", "900")
)

# ----- Apps --------------------------------------------------------------------

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "mozilla_django_oidc",
    # Severino HQ
    "core",
    "projects",
    "content",
    "docs_index",
    "assets",
    "expenses",
    "receipts",
    "reports",
    "contacts",
    "control_plane",
    "search_index",
    "hq_api",
    "jobs",
] + installed_plugin_apps()

MIDDLEWARE = [
    # Before everything. An address that may not talk to HQ should not reach
    # the session store, the login form, or the audit log.
    "core.network.TrustedNetworkMiddleware",
    "core.middleware.RequestContextMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise serves /static/ in production (DEBUG=0). Must come immediately
    # after SecurityMiddleware so it can short-circuit static-file requests
    # before sessions / auth do any work.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "core.middleware.LoginRequiredMiddleware",
    "core.middleware.CurrentUserMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.csp",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.site",
                "core.context_processors.nav",
                "core.context_processors.auth_config",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# ----- Database ----------------------------------------------------------------

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get(
            "SEVERINO_DATABASE_PATH", str(BASE_DIR / "data" / "severino.sqlite3")
        ),
        "OPTIONS": {
            "init_command": (
                "PRAGMA journal_mode=WAL;"
                "PRAGMA synchronous=NORMAL;"
                "PRAGMA foreign_keys=ON;"
            ),
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
            # fails with "database table is locked" -- an error production
            # cannot produce. A file gives the suite the same journal mode,
            # lock and timeout as the running host, for a couple of seconds.
            #
            # In the temporary directory rather than beside the real database:
            # `data/` is a mounted volume in production and does not exist in
            # the composed image, where the suite runs as its own admission
            # gate. Only being a file matters, not where the file is.
            "NAME": os.environ.get(
                "SEVERINO_TEST_DATABASE_PATH",
                str(Path(tempfile.gettempdir()) / "severino-test.sqlite3"),
            ),
        },
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ----- Auth --------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
# The marker matters. Without it, signing out under SSO-only redirects
# straight back into a still-valid Pocket ID session and signs the operator
# back in -- a sign-out button that visibly does nothing.
LOGOUT_REDIRECT_URL = "/accounts/login/?signed_out=1"

# Paths that are public (everything else requires login).
LOGIN_EXEMPT_URL_NAMES = {
    "login",
    "logout",
    "oidc_authentication_init",
    "oidc_authentication_callback",
}
LOGIN_EXEMPT_PATH_PREFIXES = (
    "/health/",
    "/accounts/login",
    "/accounts/logout",
    "/oidc/",
    "/static/",
    # Exempt from the session-login *redirect*, not from authentication. These
    # views read a bearer token and answer 401; a 302 to an HTML login page is
    # the wrong answer for a Shortcut, which cannot fill one in.
    "/api/",
)

# Pocket ID / OIDC SSO is how a person signs in.
SEVERINO_OIDC_ENABLED = env_bool("SEVERINO_OIDC_ENABLED")

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
SEVERINO_PASSWORD_LOGIN_ENABLED = env_bool(
    "SEVERINO_PASSWORD_LOGIN_ENABLED", default=not SEVERINO_OIDC_ENABLED
)

# The backend is removed, not merely unused: the guarantee has to hold for
# any caller of `authenticate()`, not only for the login view.
AUTHENTICATION_BACKENDS = ["core.oidc.HQOIDCAuthenticationBackend"] + (
    ["django.contrib.auth.backends.ModelBackend"]
    if SEVERINO_PASSWORD_LOGIN_ENABLED
    else []
)

SEVERINO_OIDC_ALLOWED_EMAILS = {
    email.lower() for email in env_list("SEVERINO_OIDC_ALLOWED_EMAILS")
}
SEVERINO_OIDC_ALLOWED_GROUPS = set(env_list("SEVERINO_OIDC_ALLOWED_GROUPS"))

OIDC_ISSUER = os.environ.get("SEVERINO_OIDC_ISSUER", "https://sso.jseverino.com").rstrip("/")
OIDC_RP_CLIENT_ID = os.environ.get("SEVERINO_OIDC_CLIENT_ID", "")
OIDC_RP_CLIENT_SECRET = os.environ.get("SEVERINO_OIDC_CLIENT_SECRET", "")
OIDC_RP_SCOPES = "openid profile groups"
OIDC_RP_SIGN_ALGO = "RS256"
OIDC_OP_AUTHORIZATION_ENDPOINT = f"{OIDC_ISSUER}/authorize"
OIDC_OP_TOKEN_ENDPOINT = f"{OIDC_ISSUER}/api/oidc/token"
OIDC_OP_USER_ENDPOINT = f"{OIDC_ISSUER}/api/oidc/userinfo"
OIDC_OP_JWKS_ENDPOINT = f"{OIDC_ISSUER}/.well-known/jwks.json"
OIDC_CREATE_USER = env_bool("SEVERINO_OIDC_CREATE_USER", default=True)
OIDC_USE_PKCE = True
OIDC_STORE_ACCESS_TOKEN = False
OIDC_STORE_ID_TOKEN = False
OIDC_AUTHENTICATION_CALLBACK_URL = "oidc_authentication_callback"

# Machine-client API. HQ verifies access tokens Pocket ID issued for this
# resource and mints no credential of its own, so there is nothing to revoke
# here -- revocation is done on the client in Pocket ID.
#
# Empty disables the surface fail-closed, and must: without a resource to check
# `aud` against, a token minted for any other API on the same issuer would
# verify here on signature alone.
SEVERINO_API_RESOURCE = os.environ.get("SEVERINO_API_RESOURCE", "")
# Clock skew allowance between the phone, Pocket ID and HQ. Small on purpose:
# the tokens are short-lived, and a generous window is a longer replay window.
SEVERINO_API_LEEWAY_SECONDS = int(os.environ.get("SEVERINO_API_LEEWAY_SECONDS", "30"))
# A retry key represents one machine request for this long. The record is
# durable because a process restart is exactly when an in-memory replay cache
# would fail the client that needs it.
SEVERINO_API_IDEMPOTENCY_TTL_SECONDS = int(
    os.environ.get("SEVERINO_API_IDEMPOTENCY_TTL_SECONDS", "86400")
)
if SEVERINO_API_IDEMPOTENCY_TTL_SECONDS < 60:
    raise RuntimeError("SEVERINO_API_IDEMPOTENCY_TTL_SECONDS must be at least 60.")

# Encrypts the few secrets an operator deliberately hands to HQ -- today, the
# private key of an internally signed certificate that has to reach a proxy.
# Unset, HQ refuses to hold one rather than storing it in the clear; see
# core.secrets. Not a provider credential: those stay outside the web container.
SEVERINO_SECRET_STORE_KEY = env_secret("SEVERINO_SECRET_STORE_KEY")

# Private MCP endpoint. All three settings are enforced by the ASGI boundary;
# empty hosts or a short/empty token disable MCP fail-closed.
SEVERINO_MCP_TOKEN = env_secret("SEVERINO_MCP_TOKEN")
SEVERINO_MCP_ALLOWED_HOSTS = env_list("SEVERINO_MCP_ALLOWED_HOSTS")
SEVERINO_MCP_ALLOWED_NETWORKS = env_list(
    "SEVERINO_MCP_ALLOWED_NETWORKS",
    default=["100.64.0.0/10", "fd7a:115c:a1e0::/48"],
)
SEVERINO_MCP_ALLOWED_ORIGINS = env_list("SEVERINO_MCP_ALLOWED_ORIGINS")
SEVERINO_MCP_ENABLE_WRITES = env_bool("SEVERINO_MCP_ENABLE_WRITES", False)
# Mirroring the vault documentation index is gated separately from the broad
# write flag: it is the one write wanted routinely, and bundling it meant the
# only way to enable `hq sync` was to also grant write access to expenses,
# receipts, projects, assets and content.
SEVERINO_MCP_ENABLE_DOC_SYNC = env_bool("SEVERINO_MCP_ENABLE_DOC_SYNC", False)
SEVERINO_MCP_ENABLE_PRUNE = env_bool("SEVERINO_MCP_ENABLE_PRUNE", False)
SEVERINO_MCP_ENABLE_DELETES = env_bool("SEVERINO_MCP_ENABLE_DELETES", False)
SEVERINO_MCP_ENABLE_INFRASTRUCTURE = env_bool(
    "SEVERINO_MCP_ENABLE_INFRASTRUCTURE", False
)
# Requesting a certificate is an outward action with a real-world effect, so
# it is gated on its own rather than riding along with declaring topology.
SEVERINO_MCP_ENABLE_CERT_RENEWAL = env_bool("SEVERINO_MCP_ENABLE_CERT_RENEWAL", False)
SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS = env_bool(
    "SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS", False
)


# ----- I18N --------------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("DJANGO_TIME_ZONE", "America/Chicago")
USE_I18N = True
USE_TZ = True

# Custom formatting to match operator preference: 5/23/26 5:49 PM
DATE_FORMAT = "n/j/y"
DATETIME_FORMAT = "n/j/y g:i A"
SHORT_DATE_FORMAT = "n/j/y"
SHORT_DATETIME_FORMAT = "n/j/y g:i A"


# ----- Static & media ----------------------------------------------------------

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = Path(os.environ.get("DJANGO_STATIC_ROOT", str(BASE_DIR / "staticfiles")))

# WhiteNoise: serve compressed, far-future-cached static files in production.
# Use the non-manifest backend so a missing collectstatic run doesn't 500 the
# whole site; we accept that asset URLs aren't fingerprinted.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

# Media (uploaded receipts) lives OUTSIDE the app code in production.
# Receipt files are served only through an auth-protected view, never via MEDIA_URL.
MEDIA_ROOT = Path(
    os.environ.get("SEVERINO_MEDIA_ROOT", str(BASE_DIR / "var" / "media"))
)
MEDIA_URL = "/_internal-media/"  # not actually exposed; receipts use protected view

EXPORTS_ROOT = Path(
    os.environ.get("SEVERINO_EXPORTS_ROOT", str(BASE_DIR / "var" / "exports"))
)

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
        "json": {"()": "core.logging.JsonFormatter"},
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
# The canonical name of this platform, for anything that leaves it -- a printed
# page, an exported file. Deliberately not derived from the request: a brief
# printed from a laptop is the same document as one printed from the server,
# and it should not tell a reader to visit a host they cannot reach.
SEVERINO_SITE_HOST = os.environ.get("SEVERINO_SITE_HOST", "hq.jseverino.com")
SEVERINO_FISCAL_YEAR_START_MONTH = int(
    os.environ.get("SEVERINO_FISCAL_YEAR_START_MONTH", "1")
)
if not 1 <= SEVERINO_FISCAL_YEAR_START_MONTH <= 12:
    raise RuntimeError("SEVERINO_FISCAL_YEAR_START_MONTH must be between 1 and 12.")

SEVERINO_DOC_REVIEW_INTERVAL_DAYS = int(
    os.environ.get("SEVERINO_DOC_REVIEW_INTERVAL_DAYS", "180")
)
if SEVERINO_DOC_REVIEW_INTERVAL_DAYS < 1:
    raise RuntimeError("SEVERINO_DOC_REVIEW_INTERVAL_DAYS must be at least 1.")

# Cloudflare D1 — the jseverino.com contact-form submissions live in a
# Cloudflare D1 database, not HQ's SQLite. The contacts app reads/writes it
# over the D1 HTTP API.
CLOUDFLARE_ACCOUNT_ID = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
CLOUDFLARE_D1_DATABASE_ID = os.environ.get("CLOUDFLARE_D1_DATABASE_ID", "")
CLOUDFLARE_API_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")


# Ensure the directories we depend on exist at startup.
for _d in (
    Path(DATABASES["default"]["NAME"]).parent,
    MEDIA_ROOT,
    EXPORTS_ROOT,
    STATIC_ROOT,
):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Don't crash at import time on a read-only filesystem; the user will see
        # a clear error from Django when the resource is actually accessed.
        pass

# ----- Content index (jseverino.com published-writeups pull) -------------------
# HQ reflects what is live on the public site, mirroring the GitHub refresh:
# fetch an already-public JSON index over HTTP, gated by a Cloudflare Access
# service token. See content/content_sync.py.
CONTENT_INDEX_URL = os.environ.get(
    "CONTENT_INDEX_URL", "https://jseverino.com/content-index.json"
)
CONTENT_INDEX_PROJECT_SLUG = os.environ.get(
    "CONTENT_INDEX_PROJECT_SLUG", "jseverino-site"
)
CF_ACCESS_CLIENT_ID = env_secret("CF_ACCESS_CLIENT_ID")
CF_ACCESS_CLIENT_SECRET = env_secret("CF_ACCESS_CLIENT_SECRET")

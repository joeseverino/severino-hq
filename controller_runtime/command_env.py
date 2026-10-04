"""Authentication boundaries for controller subprocesses."""

import os

# What a child may inherit: what locates tools, locale and temp space, and TLS
# roots. Credentials are passed per call, never inherited, so certbot, ssh and
# op never see another connection's secret.
CHILD_ENVIRONMENT = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
)


def command_environment(overrides: dict[str, str] | None) -> dict[str, str]:
    environment = {name: os.environ[name] for name in CHILD_ENVIRONMENT if name in os.environ}
    environment.update(overrides or {})
    return environment

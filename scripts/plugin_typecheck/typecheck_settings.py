"""Settings an extension's type check loads: the host's, with that extension installed.

django-stubs imports a settings module to learn the installed apps and models.
scripts/plugin-typecheck.sh puts this on the path, and check-plugin.sh has
already named the package in SEVERINO_HQ_PLUGINS.
"""

import os

os.environ.setdefault("DJANGO_SECRET_KEY", "type-checker-only-key-0123456789abcdef0123456789abcdef")

from hq.config.settings import *  # noqa: F403 - every setting, as the host defines it

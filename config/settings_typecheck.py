"""Settings the type checker loads (mypy.ini): the real ones, with no extensions.

django-stubs imports a settings module to learn the installed apps and models.
These are the host's own settings, given the throwaway key a process without
DEBUG needs, and with the extension set cleared so the result describes this
repository and nothing composed onto it.
"""

import os

os.environ.setdefault(
    "DJANGO_SECRET_KEY", "type-checker-only-key-0123456789abcdef0123456789abcdef"
)
os.environ["SEVERINO_HQ_PLUGINS"] = ""

from config.settings import *  # noqa: E402,F403

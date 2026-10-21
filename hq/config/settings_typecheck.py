"""Settings the type checker loads: the host's own, with the extension set it is given.

django-stubs imports a settings module to learn the installed apps and models.
These are the host's settings, given the throwaway key and the database path
they refuse to load without; type checking opens no database. The host's mypy gate gives no extension set, so its result describes
this repository alone; an extension's type check (tools/pipeline/plugin-typecheck.sh)
runs with that extension named, so its models are known.
"""

import os

os.environ.setdefault("DJANGO_SECRET_KEY", "type-checker-only-key-0123456789abcdef0123456789abcdef")
os.environ.setdefault("SEVERINO_DATABASE_PATH", os.devnull)
os.environ.setdefault("SEVERINO_HQ_PLUGINS", "")

from hq.config.settings import *  # noqa: F403 - every setting, as the host defines it

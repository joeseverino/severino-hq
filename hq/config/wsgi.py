"""WSGI entrypoint for Severino HQ."""

import os

lazy from django.core.wsgi import get_wsgi_application

from hq.config.imports import lazily

lazily()
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hq.config.settings")

application = get_wsgi_application()

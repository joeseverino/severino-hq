"""Fast native-ASGI delivery for versioned static assets."""

import os
from urllib.parse import parse_qs

from django.conf import settings
from django.contrib.staticfiles import finders
from starlette.staticfiles import StaticFiles


class CachedStaticFiles(StaticFiles):
    """Cache versioned assets permanently and ordinary assets briefly.

    Never while serving live (``STATIC_LIVE``), and that exception is
    load-bearing: the version token hashes the source tree once, so a
    far-future cache would pin whatever bytes it first saw and every later edit
    would look like the application not running the code on disk. Production
    serves the tree it collects on every boot, so token and bytes agree there,
    and that is the caching this keeps.
    """

    def lookup_path(self, path):
        # Live, the source trees answer first, through the same finders
        # collectstatic reads, so there is nothing to collect after an edit.
        # The finders refuse a path outside their roots; the parent refuses an
        # absolute one, so that check runs before them.
        if settings.STATIC_LIVE and not path.startswith(("/", "\\")):
            found = finders.find(path)
            if found:
                return found, os.stat(found)
        return super().lookup_path(path)

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            query = parse_qs(scope.get("query_string", b""))
            response.headers["Cache-Control"] = (
                "no-cache"
                if settings.STATIC_LIVE
                else "public, max-age=31536000, immutable"
                if b"v" in query
                else "public, max-age=3600"
            )
            # This mount sits above the Django stack, so the middleware that
            # sets the same header on every other response never sees it. An
            # asset is the easiest thing for another origin to pull in, and a
            # boundary with one silent exception is the exception people find.
            response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

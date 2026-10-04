"""Fast native-ASGI delivery for content-named static assets."""

import os

from django.conf import settings
from django.contrib.staticfiles import finders
from django.contrib.staticfiles.storage import staticfiles_storage
from starlette.staticfiles import StaticFiles
from whitenoise.storage import CompressedManifestStaticFilesStorage


class HashedStaticStorage(CompressedManifestStaticFilesStorage):
    """Django's content-hashed names for every collected asset.

    A name collectstatic has not produced (a checkout that never collected, a
    test run) keeps its plain name rather than failing the page. Collecting
    stays strict: a stylesheet that references a missing file still fails
    ``collectstatic``, which resolves references through ``_stored_name``.
    """

    manifest_strict = False

    def stored_name(self, name):
        try:
            return super().stored_name(name)
        except ValueError:
            return name


def content_named(path: str) -> bool:
    """Whether ``path`` is a collected, content-hashed asset name."""

    return path in getattr(staticfiles_storage, "hashed_files", {}).values()


class CachedStaticFiles(StaticFiles):
    """Cache content-named assets permanently and ordinary assets briefly.

    Never while serving live (``STATIC_LIVE``): the source trees it reads
    change under the same plain name. Production serves the tree it collects
    on every boot, where a hashed name only ever means one set of bytes.
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
            response.headers["Cache-Control"] = (
                "no-cache"
                if settings.STATIC_LIVE
                else "public, max-age=31536000, immutable"
                if content_named(path)
                else "public, max-age=3600"
            )
            # This mount sits above the Django stack, so the middleware that
            # sets the same header on every other response never sees it. An
            # asset is the easiest thing for another origin to pull in, and a
            # boundary with one silent exception is the exception people find.
            response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

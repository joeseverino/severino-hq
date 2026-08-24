"""Fast native-ASGI delivery for versioned static assets."""

from urllib.parse import parse_qs

from django.conf import settings
from starlette.staticfiles import StaticFiles


class CachedStaticFiles(StaticFiles):
    """Cache versioned assets permanently and ordinary assets briefly.

    Never in development, though, and that exception is load-bearing. The
    version token is a hash of the *source* tree, while this mount serves the
    *collected* one, and in development those two are only in step just after
    `collectstatic`. Edit a script, load a page before collecting, and the
    browser is handed the new URL with the old bytes -- and told to keep them
    forever. Every later edit is then invisible behind a cache entry that will
    never be revalidated, which presents as the application simply not running
    the code on disk.

    Production collects on every boot, so the two trees are never out of step
    there; the far-future caching that matters is the caching this keeps.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            query = parse_qs(scope.get("query_string", b""))
            response.headers["Cache-Control"] = (
                "no-cache"
                if settings.DEBUG
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

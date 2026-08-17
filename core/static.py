"""Fast native-ASGI delivery for versioned static assets."""

from urllib.parse import parse_qs

from starlette.staticfiles import StaticFiles


class CachedStaticFiles(StaticFiles):
    """Cache versioned assets permanently and ordinary assets briefly."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            query = parse_qs(scope.get("query_string", b""))
            response.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable"
                if b"v" in query
                else "public, max-age=3600"
            )
        return response

"""Django's answers, compressed as they are sent, in the smallest coding the client takes.

Collected assets never pass through here: the image build compressed each one
once (core.static). The proxy in front of HQ gzips only what arrives without a
coding, so a response encoded here crosses it as it is.

Brotli is preferred over gzip because every browser HQ serves sends br over
HTTPS, and at quality 5 a page is smaller than gzip's and costs less CPU than
gzip at 6. Zstandard is left off: starlette-compress ranks it first whenever a
client offers it, and at a comparable cost a page is larger in it than in br.

BREACH: a compressed page that reflects input beside a secret leaks the secret
through its length to whoever can watch the encrypted traffic and make the
browser ask many times. Brotli is no different from gzip there. The secrets a
page carries are each fresh per response, so a guess at one says nothing of the
next: Django masks the CSRF token, and the CSP nonce is new each time. The
session cookie is never in a body. What is left is the operator's own data, on
a host reached only over the tailnet, behind cookies that are SameSite=Lax.
Nothing here pads a body to a random length, so no property above leans on one.
"""

from starlette.datastructures import Headers, MutableHeaders
from starlette_compress import CompressMiddleware


class Compressed:
    """An ASGI application's answers compressed, each encoded one with a weak validator.

    An encoded body is not the bytes a strong ETag vouched for (RFC 9110,
    section 8.8.1), so the tag is weakened as the coding is set, as nginx and
    Django's GZipMiddleware do. A validator presented back in its weak form
    still matches: If-None-Match compares weakly.
    """

    def __init__(self, app):
        self.app = app
        self.compressing = CompressMiddleware(self._answering, zstd=False, brotli_quality=5, gzip_level=6)

    async def _answering(self, scope, receive, send):
        # Notes whether the answer came encoded, so only an encoding added here
        # weakens its validator. The compressor copies the scope, never this value.
        async def answered(message):
            if message["type"] == "http.response.start":
                scope[_ENCODED]["by_app"] = "content-encoding" in Headers(raw=message["headers"])
            await send(message)

        await self.app(scope, receive, answered)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        encoded = {"by_app": False}

        async def sending(message):
            if message["type"] == "http.response.start" and not encoded["by_app"]:
                headers = MutableHeaders(scope=message)
                validator = headers.get("etag", "")
                if "content-encoding" in headers and validator.startswith('"'):
                    headers["ETag"] = f"W/{validator}"
            await send(message)

        await self.compressing({**scope, _ENCODED: encoded}, receive, sending)


# The scope key the answer's own encoding is noted under, for this module alone.
_ENCODED = "hq.compression.encoded"

"""Django's answers, compressed as they are sent, in the smallest coding the client takes.

Collected assets were compressed once by the image build (core.static). Brotli
at quality 5 beats gzip on size and CPU; Zstandard is off, since starlette-
compress ranks it first and a page is larger in it. BREACH: docs/Security.md.
"""

import brotli
from starlette.datastructures import Headers, MutableHeaders
from starlette_compress import CompressMiddleware

# How hard a page is compressed in brotli, the coding every browser HQ serves takes.
BROTLI_QUALITY = 5


def as_sent(body: bytes) -> bytes:
    """``body`` as a browser receives it: what a budget on a page's weight reads."""

    return brotli.compress(body, quality=BROTLI_QUALITY)


class Compressed:
    """An ASGI application's answers compressed, each encoded one with a weak validator.

    An encoded body is not the bytes a strong ETag vouched for (RFC 9110,
    section 8.8.1), so the tag is weakened as the coding is set, as nginx and
    Django's GZipMiddleware do. A validator presented back in its weak form
    still matches: If-None-Match compares weakly.
    """

    def __init__(self, app):
        self.app = app
        self.compressing = CompressMiddleware(self._answering, zstd=False, brotli_quality=BROTLI_QUALITY, gzip_level=6)

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

"""Static assets: collected and compressed once at image build, served natively over ASGI."""

from pathlib import Path
from typing import override

from django.conf import settings
from django.contrib.staticfiles import finders
from django.contrib.staticfiles.storage import staticfiles_storage
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.staticfiles import StaticFiles
from whitenoise.storage import CompressedManifestStaticFilesStorage


class HashedStaticStorage(CompressedManifestStaticFilesStorage):
    """Django's content-hashed names for every collected asset, each beside a gzip copy.

    A name collectstatic has not produced (a checkout that never collected, a
    test run) keeps its plain name rather than failing the page. Collecting
    stays strict: a stylesheet that references a missing file still fails
    ``collectstatic``, which resolves references through ``_stored_name``.
    """

    manifest_strict = False

    @override
    def stored_name(self, name):
        try:
            return super().stored_name(name)
        except ValueError:
            return name


def collected() -> bool:
    """Whether the assets this process serves were collected: the manifest names some."""

    return bool(getattr(staticfiles_storage, "hashed_files", None))


def content_named(path: str) -> bool:
    """Whether ``path`` is a collected, content-hashed asset name."""

    return path in getattr(staticfiles_storage, "hashed_files", {}).values()


def accepts_gzip(scope) -> bool:
    """Whether the request's Accept-Encoding takes gzip (RFC 9110, section 12.5.3)."""

    for offer in Headers(scope=scope).get("accept-encoding", "").lower().split(","):
        coding, _, weight = offer.partition(";")
        if coding.strip() not in ("gzip", "*"):
            continue
        quality = weight.strip().removeprefix("q=")
        try:
            return float(quality or 1) > 0
        except ValueError:
            return False
    return False


class CachedStaticFiles(StaticFiles):
    """Collected assets, as the bytes the image build left: compressed once, never per request.

    A request that takes gzip is answered with the ``.gz`` copy collectstatic
    wrote beside the asset. Content-named assets are cached permanently and
    ordinary ones briefly.

    Never while serving live (``STATIC_LIVE``): the source trees it reads
    change under the same plain name and have no compressed copies. Production
    serves the tree its image collected, where a hashed name only ever means
    one set of bytes.
    """

    @override
    def lookup_path(self, path):
        # Live, the source trees answer first, through the same finders
        # collectstatic reads, so there is nothing to collect after an edit.
        # The finders refuse a path outside their roots; the parent refuses an
        # absolute one, so that check runs before them.
        if settings.STATIC_LIVE and not path.startswith(("/", "\\")):
            found = finders.find(path)
            if found:
                return found, Path(found).stat()
        return super().lookup_path(path)

    async def precompressed(self, path, scope):
        """The asset's gzip copy as its response, or None when there is none to send."""

        if settings.STATIC_LIVE or not accepts_gzip(scope):
            return None
        try:
            # The media type is the asset's: mimetypes reads ".css.gz" as CSS, gzip-encoded.
            response = await super().get_response(path + ".gz", scope)
        except HTTPException as refused:
            if refused.status_code != 404:
                raise
            return None
        if response.status_code == 200:
            response.headers["Content-Encoding"] = "gzip"
        return response

    @override
    async def get_response(self, path, scope):
        response = await self.precompressed(path, scope) or await super().get_response(path, scope)
        if not settings.STATIC_LIVE:
            # One name, two representations: a cache keeps them apart.
            response.headers["Vary"] = "Accept-Encoding"
        # A revalidation carries the policy the full answer would have.
        if response.status_code in (200, 304):
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

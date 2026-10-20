"""Static assets: collected and compressed once at image build, served natively over ASGI."""

import re
from pathlib import Path
from typing import override

from django.conf import settings
from django.contrib.staticfiles import finders
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.files.base import ContentFile
from rjsmin import jsmin
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.staticfiles import StaticFiles
from whitenoise.storage import CompressedManifestStaticFilesStorage
lazy from django.contrib.auth.decorators import login_not_required
lazy from django.http import HttpResponsePermanentRedirect
lazy from django.templatetags.static import static

# A quoted string (kept whole, so a "/*" in one is not a comment) or a comment.
# A comment that opens "/*!" is a licence, and is kept.
_CSS_STRING_OR_COMMENT = re.compile(r"""("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|/\*(?!!).*?\*/""", re.DOTALL)


def without_comments(css: str) -> str:
    """A stylesheet without its comments or the blank lines they leave.

    Line breaks stay, so the served file still reads rule by rule in the inspector.
    """

    kept = _CSS_STRING_OR_COMMENT.sub(lambda found: found.group(1) or "", css)
    return "".join(line.rstrip() + "\n" for line in kept.splitlines() if line.strip())


class HashedStaticStorage(CompressedManifestStaticFilesStorage):
    """Django's content-hashed names for every collected asset, each beside a gzip copy.

    A name collectstatic has not produced (a checkout that never collected, a
    test run) keeps its plain name rather than failing the page. Collecting
    stays strict: a stylesheet that references a missing file still fails
    ``collectstatic``, which resolves references through ``_stored_name``.
    """

    manifest_strict = False

    @override
    def _save(self, name, content):
        # collectstatic writes every copy through here, so the hash and the gzip
        # copy match the bytes served.
        if name.endswith((".css", ".js")) and not name.startswith("vendor/"):
            # Rewound first: hashing the file read it to its end.
            content.seek(0)
            text = content.read().decode()
            served = without_comments(text) if name.endswith(".css") else jsmin(text, keep_bang_comments=True)
            content = ContentFile(served.encode())
        return super()._save(name, content)

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
    """Whether the request's Accept-Encoding takes gzip (RFC 9110, section 12.5.3).

    gzip named outright decides, wherever it stands; ``*`` speaks only for a
    coding the header does not name. A weight that is not a number refuses.
    """

    weights: dict[str, float] = {}
    for offer in Headers(scope=scope).get("accept-encoding", "").lower().split(","):
        coding, _, weight = offer.partition(";")
        quality = weight.strip().removeprefix("q=")
        try:
            weights[coding.strip()] = float(quality or 1)
        except ValueError:
            weights[coding.strip()] = 0
    return weights.get("gzip", weights.get("*", 0)) > 0


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


def root_icon(name: str):
    """A view for an icon a browser asks for at the site's root whatever the page
    links (``/favicon.ico``, iOS's ``/apple-touch-icon.png``): a permanent
    redirect to the collected file, so each client asks once. Open before sign
    in, since a browser asks on the sign-in page too."""

    @login_not_required
    def view(request):
        return HttpResponsePermanentRedirect(static(name))

    return view

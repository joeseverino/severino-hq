from application.domains import domain_navigation
from functools import cache
import hashlib
import os
from pathlib import Path
import time

from django.conf import settings


def _asset_fingerprint() -> str:
    """Content fingerprint for every bundle loaded by the application shell.

    STORAGES deliberately uses the non-manifest static backend, so asset URLs
    carry no content hash -- while WhiteNoise serves them with far-future cache
    headers. Without a version token a deployed asset change is invisible to a
    browser holding a cached copy. Content—not mtimes—means an image rebuild
    neither busts unchanged assets nor retains changed JavaScript.
    """
    digest = hashlib.sha256()
    root = Path(settings.BASE_DIR) / "static"
    for relative in (
        "css/app.css",
        "img/apple-touch-icon.png",
        "img/favicon.ico",
        "img/favicon.svg",
        "js/app.js",
        "js/tables.js",
    ):
        try:
            digest.update((root / relative).read_bytes())
        except OSError:
            return "0"
    return digest.hexdigest()[:12]


@cache
def _production_asset_version() -> str:
    return _asset_fingerprint()


@cache
def _development_asset_version() -> str:
    """A token that belongs to this run rather than to the file contents.

    In development the fingerprint above is actively harmful. It hashes the
    source tree, while the mount serves the collected one, so between an edit
    and the next `collectstatic` the page advertises a URL derived from the new
    bytes and the server answers it with the old ones. A browser that asks in
    that window caches the wrong body against the right URL, and because the
    hash has already moved on, nothing will ever produce a different URL to
    dislodge it. The symptom is not a caching one: it is the application
    running code that is no longer on disk, indefinitely.

    A per-run token cannot land in that state -- the next start has a different
    URL whatever happened during the last one -- and `core.static` sends
    `no-cache` in development anyway, so edits still appear without a restart.
    """

    return f"dev{os.getpid()}{int(time.time())}"


def _asset_version() -> str:
    # Development separates one run from the next and revalidates within a run.
    # Production computes a content fingerprint once, avoiding filesystem reads
    # in every template context.
    return (
        _development_asset_version() if settings.DEBUG else _production_asset_version()
    )


def site(request):
    return {
        "SITE_NAME": getattr(settings, "SEVERINO_SITE_NAME", "Severino HQ"),
        "SITE_HOST": getattr(settings, "SEVERINO_SITE_HOST", "hq.jseverino.com"),
        "ASSET_VERSION": _asset_version(),
    }


def nav(request):
    """Primary-nav entries, grouped, with the active section flagged.

    Returns a flat ordered sequence of entries. Each is either a link
    (``kind="item"``) or a dropdown (``kind="group"``) holding links. An item
    with no group renders inline in the bar; a named group collects its items
    into one dropdown, so the bar stays a fixed handful of controls no matter
    how many sections exist.

    The entries themselves are not defined here -- they are derived from
    ``application.domains``, which is where a section declares itself once. This
    function only decides what is *currently* active.
    """
    match = request.resolver_match
    namespace = getattr(match, "namespace", "") or ""
    url_name = getattr(match, "url_name", "") or ""
    current_route = f"{namespace}:{url_name}" if namespace else url_name

    entries: list[dict] = []
    groups: dict[str, dict] = {}
    for nav_item in domain_navigation():
        item = {
            "label": nav_item.label,
            "url": nav_item.route,
            # The exact route, not its namespace. A section with more than one
            # entry shares one namespace, so matching on that lit every entry in
            # the dropdown at once. An item with no namespace (the dashboard,
            # which lives at the root) matches on the bare url_name.
            "active": (
                (not namespace and url_name == nav_item.route)
                if not nav_item.namespace
                else current_route == nav_item.route
            ),
        }
        if not nav_item.group:
            entries.append({"kind": "item", "order": nav_item.order, **item})
            continue
        if nav_item.group not in groups:
            groups[nav_item.group] = {
                "kind": "group",
                "label": nav_item.group,
                "items": [],
                "active": False,
                "order": nav_item.order,
            }
            entries.append(groups[nav_item.group])
        groups[nav_item.group]["items"].append(item)
        # The group is active for anywhere in the section, including pages that
        # have no nav entry of their own -- otherwise opening one makes the
        # current section vanish from the bar.
        groups[nav_item.group]["active"] = (
            groups[nav_item.group]["active"] or namespace == nav_item.namespace
        )

    return {"nav_entries": entries}


def auth_config(request):
    return {"OIDC_ENABLED": getattr(settings, "SEVERINO_OIDC_ENABLED", False)}


def connection(request):
    """Which network this request came over, for the header badge.

    Address arithmetic and the configured proxy ranges only -- no query and no
    inventory. The badge is on every page, so anything it costs is a cost every
    page pays; the panel behind it does the expensive part, and only when
    somebody opens it. Applying the proxy rule here keeps an opaque chain from
    being mislabeled as a local caller before the panel opens.
    """

    from application.connection import channel_for_request

    return {"CONNECTION_CHANNEL": channel_for_request(request)}

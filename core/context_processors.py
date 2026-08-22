from application.domains import domain_navigation
from functools import cache
import hashlib
from pathlib import Path

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


def _asset_version() -> str:
    # Development reflects edits without a restart. Production computes once,
    # avoiding filesystem reads in every template context.
    return _asset_fingerprint() if settings.DEBUG else _production_asset_version()


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

    Arithmetic on one address and nothing else -- no query, no settings read,
    no inventory. The badge is on every page, so anything it costs is a cost
    every page pays; the panel behind it does the expensive part, and only when
    somebody opens it.
    """

    from application.connection import channel_of
    from core.network import client_ip

    return {"CONNECTION_CHANNEL": channel_of(client_ip(request))}

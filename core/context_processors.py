from application.plugins import plugin_navigation
from pathlib import Path

from django.conf import settings

# Primary-nav definition: (label, url name, namespace, group).
# A ``None`` namespace matches on the bare url_name instead (the dashboard
# lives at the root and has no namespace).
#
# ``group`` is the scaling mechanism. An empty group renders inline in the bar;
# a named group collects its items into one dropdown. The bar previously grew
# by one entry per section and silently truncated the tail behind a hidden
# scrollbar, so sections could disappear with no affordance. Grouping keeps the
# bar a fixed handful of controls no matter how many sections exist, and leaves
# inline space for the surfaces an operator actually opens every day.
NAV_ITEMS = (
    ("Dashboard", "dashboard", None, ""),
    ("Projects", "projects:list", "projects", "Business"),
    ("Content", "content:list", "content", "Business"),
    ("Docs", "docs_index:list", "docs_index", "Business"),
    ("Assets", "assets:list", "assets", "Business"),
    ("Contacts", "contacts:list", "contacts", "Business"),
    ("Expenses", "expenses:list", "expenses", "Finance"),
    ("Receipts", "receipts:list", "receipts", "Finance"),
    ("Reports", "reports:dashboard", "reports", "Finance"),
    ("Infrastructure", "control_plane:list", "control_plane", "System"),
    ("Audit", "core:audit_list", "core", "System"),
)


def _asset_version() -> str:
    """Cache-busting token for the style bundle.

    STORAGES deliberately uses the non-manifest static backend, so asset URLs
    carry no content hash -- while WhiteNoise serves them with far-future cache
    headers. Without a version token a deployed stylesheet change is invisible
    to any browser holding a cached copy, which looks exactly like the change
    never shipped. Derived from the file's mtime and size: no build step, and it
    changes precisely when the file does.
    """
    path = Path(settings.BASE_DIR) / "static" / "css" / "app.css"
    try:
        stat = path.stat()
    except OSError:
        return "0"
    return f"{int(stat.st_mtime)}-{stat.st_size}"


def site(request):
    return {
        "SITE_NAME": getattr(settings, "SEVERINO_SITE_NAME", "Severino HQ"),
        "ASSET_VERSION": _asset_version(),
    }


def nav(request):
    """Primary-nav entries, grouped, with the active section flagged.

    Returns a flat ordered sequence of entries. Each is either a link
    (``kind="item"``) or a dropdown (``kind="group"``) holding links. Extension
    surfaces default to no group, so they render inline and stay one click
    away; the host's own sections are grouped so the bar does not grow without
    bound.
    """
    match = request.resolver_match
    namespace = getattr(match, "namespace", "") or ""
    url_name = getattr(match, "url_name", "") or ""

    definitions = [
        *NAV_ITEMS,
        *(
            (item.label, item.route, item.namespace, getattr(item, "group", ""))
            for item in plugin_navigation()
        ),
    ]

    entries: list[dict] = []
    groups: dict[str, dict] = {}
    for label, route, ns, group in definitions:
        item = {
            "label": label,
            "url": route,
            # A None namespace means "match the bare url_name", but the name
            # alone is ambiguous: reports:dashboard also has url_name
            # "dashboard", which lit up both tabs at once. Require the
            # namespace to actually be empty.
            "active": (
                (not namespace and url_name == "dashboard")
                if ns is None
                else (namespace == ns)
            ),
        }
        if not group:
            entries.append({"kind": "item", **item})
            continue
        if group not in groups:
            groups[group] = {"kind": "group", "label": group, "items": [], "active": False}
            entries.append(groups[group])
        groups[group]["items"].append(item)
        # A collapsed group must still show that the current page lives inside
        # it, otherwise the active section vanishes from the bar entirely.
        groups[group]["active"] = groups[group]["active"] or item["active"]

    return {"nav_entries": entries}


def auth_config(request):
    return {"OIDC_ENABLED": getattr(settings, "SEVERINO_OIDC_ENABLED", False)}

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
    ("Dashboard", "dashboard", None, "", 0),
    ("Projects", "projects:list", "projects", "Business", 10),
    ("Content", "content:list", "content", "Business", 11),
    ("Docs", "docs_index:list", "docs_index", "Business", 12),
    ("Assets", "assets:list", "assets", "Business", 13),
    ("Contacts", "contacts:list", "contacts", "Business", 14),
    ("Expenses", "expenses:list", "expenses", "Finance", 20),
    ("Receipts", "receipts:list", "receipts", "Finance", 21),
    ("Reports", "reports:dashboard", "reports", "Finance", 22),
    # Deliberately last. System is where an operator goes to look at the
    # machinery, not to do the day's work, so it sits after every section that
    # holds actual work -- including sections added later by an extension.
    ("Infrastructure", "control_plane:list", "control_plane", "System", 900),
    ("Audit", "core:audit_list", "core", "System", 901),
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

    definitions = sorted(
        [
            *NAV_ITEMS,
            *(
                (
                    item.label,
                    item.route,
                    item.namespace,
                    getattr(item, "group", ""),
                    item.order,
                )
                for item in plugin_navigation()
            ),
        ],
        key=lambda definition: (definition[4], definition[0]),
    )
    current_route = f"{namespace}:{url_name}" if namespace else url_name

    entries: list[dict] = []
    groups: dict[str, dict] = {}
    for label, route, ns, group, order in definitions:
        item = {
            "label": label,
            "url": route,
            # The exact route, not its namespace. A section with more than one
            # entry shares one namespace, so matching on that lit every entry in
            # the dropdown at once.
            "active": (
                (not namespace and url_name == "dashboard")
                if ns is None
                else current_route == route
            ),
        }
        if not group:
            entries.append({"kind": "item", "order": order, **item})
            continue
        if group not in groups:
            groups[group] = {
                "kind": "group",
                "label": group,
                "items": [],
                "active": False,
                "order": order,
            }
            entries.append(groups[group])
        groups[group]["items"].append(item)
        # The group is active for anywhere in the section, including pages that
        # have no nav entry of their own -- otherwise opening one makes the
        # current section vanish from the bar.
        groups[group]["active"] = groups[group]["active"] or namespace == ns

    return {"nav_entries": entries}


def auth_config(request):
    return {"OIDC_ENABLED": getattr(settings, "SEVERINO_OIDC_ENABLED", False)}

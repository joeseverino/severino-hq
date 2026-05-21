from django.conf import settings

# Primary-nav definition: (label, url name, namespace).
# A ``None`` namespace matches on the bare url_name instead (the dashboard
# lives at the root and has no namespace).
NAV_ITEMS = (
    ("Dashboard", "dashboard", None),
    ("Projects", "projects:list", "projects"),
    ("Content", "content:list", "content"),
    ("Docs", "docs_index:list", "docs_index"),
    ("Assets", "assets:list", "assets"),
    ("Expenses", "expenses:list", "expenses"),
    ("Receipts", "receipts:list", "receipts"),
    ("Contacts", "contacts:list", "contacts"),
    ("Reports", "reports:dashboard", "reports"),
    ("Audit", "core:audit_list", "core"),
)


def site(request):
    return {
        "SITE_NAME": getattr(settings, "SEVERINO_SITE_NAME", "Severino HQ"),
    }


def nav(request):
    """Primary-nav items with the active section flagged."""
    match = request.resolver_match
    namespace = getattr(match, "namespace", "") or ""
    url_name = getattr(match, "url_name", "") or ""
    items = [
        {
            "label": label,
            "url": route,
            "active": (url_name == "dashboard") if ns is None else (namespace == ns),
        }
        for label, route, ns in NAV_ITEMS
    ]
    return {"nav_items": items}

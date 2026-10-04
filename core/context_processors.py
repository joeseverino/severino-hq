from application.domains import domain_navigation

from django.conf import settings


def site(request):
    return {
        "SITE_NAME": getattr(settings, "SEVERINO_SITE_NAME", "Severino HQ"),
        "SITE_HOST": getattr(settings, "SEVERINO_SITE_HOST", "localhost"),
    }


def nav(request):
    """Primary-nav entries, grouped, with the active section flagged.

    Returns a flat ordered sequence of entries. Each is either a link
    (``kind="item"``) or a dropdown (``kind="group"``) holding links. An item
    with no group renders inline in the bar; a named group collects its items
    into one dropdown, so the bar stays a fixed handful of controls no matter
    how many sections exist.

    The entries themselves are not defined here: they are derived from
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
            # entry shares one namespace, so matching on that would light every
            # entry in the dropdown at once. An item with no namespace (the dashboard,
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
        # have no nav entry of their own, otherwise opening one makes the
        # current section vanish from the bar.
        # An item with no namespace shares one with every root page, so it can
        # only light its group by being the page itself.
        groups[nav_item.group]["active"] = (
            groups[nav_item.group]["active"]
            or item["active"]
            or bool(nav_item.namespace and namespace == nav_item.namespace)
        )

    return {"nav_entries": entries}


def auth_config(request):
    return {"OIDC_ENABLED": getattr(settings, "SEVERINO_OIDC_ENABLED", False)}


def connection(request):
    """Which network this request came over, for the header badge.

    Address arithmetic and the configured proxy ranges only: no query and no
    inventory. The badge is on every page, so anything it costs is a cost every
    page pays; the panel behind it does the expensive part, and only when
    somebody opens it. Applying the proxy rule here keeps an opaque chain from
    being mislabeled as a local caller before the panel opens.
    """

    from application.request_channel import channel_for_request

    return {"CONNECTION_CHANNEL": channel_for_request(request)}


def agent_access(request):
    """Whether agents are paused, read lazily so templates without the menu cost nothing."""

    user = getattr(request, "user", None)
    if not (user and user.is_authenticated):
        return {}
    from django.utils.functional import SimpleLazyObject

    from application.agent_access import agents_paused

    return {"agents_paused": SimpleLazyObject(agents_paused)}


def appearance(request):
    """The theme `<html>` is drawn in, and the choices the menu offers.

    Lazy, so a fragment that never draws `<html>` never asks; anybody signed
    out follows the system without a query.
    """

    from django.utils.functional import SimpleLazyObject

    from application.appearance import Theme, theme_for

    user = getattr(request, "user", None)
    return {
        "THEME": SimpleLazyObject(lambda: theme_for(user)),
        "THEME_CHOICES": Theme.choices,
    }

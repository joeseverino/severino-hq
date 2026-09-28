"""Links out of HQ.

Each provider's own console and the operator's declared links, as the
navigation offers them.
"""

from __future__ import annotations

from control_plane.models import ProviderConnection


# Declared next to the domains that emit them, so a gateway can import the
# record without importing this reader. Re-exported here as the one name
# callers already use.


def consoles() -> tuple[tuple[str, str, str], ...]:
    """Connections that are a thing you can open, as (label, sub, url).

    A console and an API base are both URLs and only one is worth a link. Told
    apart by the shape a credential's endpoint already has: an API is reached at
    a path (a version, a prefix) and a console is reached at the host
    itself. So a proxy's web interface is offered and a DNS API is not, without
    a list here naming either.

    Nothing is hand-authored. A URL written into this repository is one
    deployment's address published to everyone who clones it, and stale for the
    deployment it belonged to.
    """

    from urllib.parse import urlsplit

    from control_plane.connection_kinds import CONNECTION_LABELS

    from .labels import human_label

    found = []
    for connection in ProviderConnection.objects.all():
        endpoint = connection.endpoint.strip()
        if not endpoint or "://" not in endpoint:
            continue
        parsed = urlsplit(endpoint)
        if parsed.path.strip("/"):
            continue
        found.append(
            (
                CONNECTION_LABELS.get(connection.provider)
                or human_label(connection.provider)
                or connection.connection_ref,
                connection.connection_ref,
                endpoint,
            )
        )
    return tuple(sorted(found))


def outward_links(user=None) -> tuple[list[dict[str, str]], bool]:
    """Everything HQ can open, and whether the operator has chosen a subset.

    Chosen rather than configured: which of these is worth a shortcut is a
    preference, and a preference belongs with the operator rather than in the
    deployment's environment. Nothing chosen means everything, because a panel
    that starts empty teaches nobody that it can be filled.
    """

    from .pins import DASHBOARD_LINK, pinned

    from django.urls import reverse

    from .published_sites import public_sites

    offered = [
        {
            "label": "Health endpoint",
            "sub": "liveness",
            "href": reverse("health_ready"),
        },
        *(
            {"label": label, "sub": sub or "console", "href": href}
            for label, sub, href in consoles()
        ),
        *(
            {"label": hostname, "sub": sub or "published", "href": href}
            for hostname, sub, href in public_sites()
        ),
        *operator_links(),
    ]
    chosen = pinned(user, DASHBOARD_LINK)
    if not chosen:
        return offered, False
    return [item for item in offered if item["href"].lower() in chosen] or offered, True


def link_choices(user=None) -> list[dict[str, object]]:
    """Every outward link, each marked with whether it has been chosen.

    The same list the panel shows, so the chooser cannot offer something the
    panel would not render or miss something it would.
    """

    from .pins import DASHBOARD_LINK, pinned

    chosen = pinned(user, DASHBOARD_LINK)
    offered, _ = outward_links(None)
    return [{**item, "chosen": item["href"].lower() in chosen} for item in offered]


def operator_links() -> list[dict[str, str]]:
    """Extra dashboard links this deployment wants, from its own environment.

    A status page or a public site is a fact about one installation and belongs
    with its other deployment facts. Malformed input is ignored rather than
    fatal: a dashboard is where an operator goes to fix things, and refusing to
    render it over a bad link is the least useful moment to fail.
    """

    import json

    from django.conf import settings

    raw = str(getattr(settings, "SEVERINO_DASHBOARD_LINKS", "") or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    return [
        {
            "label": str(item.get("label", ""))[:80],
            "sub": str(item.get("sub", ""))[:80],
            "href": str(item.get("href", ""))[:500],
        }
        for item in parsed
        if isinstance(item, dict) and str(item.get("href", "")).startswith("http")
    ]

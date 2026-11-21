"""The examples of the connection panel's parts: the peering, the identity and the link."""

from hq.platform.core.template_parts import Example
lazy from hq.platform.application.request_channel import NETWORK_CHANNEL, TAILNET_CHANNEL
lazy from hq.platform.application.request_connection import Connection, Peering
lazy from hq.platform.application.request_headers import RESPONSE_CHECKS
lazy from hq.platform.application.request_identity import Identity
lazy from hq.platform.application.request_standard import RequestFacts, ServingDeviceResolution
lazy from hq.platform.application.standards import Check, Reading, measure
lazy from hq.platform.application.tailnet import Device
lazy from hq.platform.application.tailnet_presence import Presence


def _connection(channel=TAILNET_CHANNEL, *, path="direct", held=True, linked=True, failing=False):
    """A request as the connection panel reads it: from an example laptop to
    an example HQ, over ``path`` (``direct``, ``relayed``, or ``""`` for no
    tailnet reading), its checks confirmed when ``held`` and one not yet
    read otherwise, or one read and not holding when ``failing``, each end
    leading to its machine when ``linked``."""

    laptop = Device("example-laptop", addresses=("192.0.2.10",), user="example-person@example.com")
    hq = Device("example-hq", addresses=("192.0.2.20",))
    presence = (
        Presence(
            public_key="example-key",
            last_handshake="2026-01-01T00:00:00Z",
            direct_endpoint="198.51.100.40:41641" if path == "direct" else "",
            relay="" if path == "direct" else "example-region",
        )
        if path
        else None
    )
    tailnet = channel is TAILNET_CHANNEL
    facts = RequestFacts(
        request=None,
        address="192.0.2.10",
        channel=channel,
        device=laptop if tailnet else None,
        serving=ServingDeviceResolution(hq if tailnet else None, verified=tailnet, basis="example"),
        identity=Identity(username="example", tailnet_user=laptop.user if tailnet else ""),
        secure=True,
        host="hq.example.com",
        presence=presence,
    )
    detail = "An example check."
    reading = (
        Reading.unmet(detail=detail)
        if failing
        else Reading.met(detail=detail)
        if held
        else Reading.unavailable("", detail=detail)
    )
    check = Check("example-check", "Example check", lambda _facts: reading, "", "", unmet="")
    return Connection(
        facts,
        measure(facts, (check,)),
        machine_url="/example/machines/example-laptop/" if linked else "",
        serves_url="/example/machines/example-hq/" if linked else "",
    )


def _posture(met, *, advanced=True):
    """The estate's posture, one example check for each reading in ``met``:
    met, not met, or ``None`` for not read."""

    def check(index, held):
        reading = Reading.unavailable("") if held is None else Reading.judged(held)
        return Check(f"example-{index}", f"Example check {index}", lambda _subject: reading, "", "", unmet="")

    return measure(None, tuple(check(index, held) for index, held in enumerate(met, 1)), enforced=advanced)


IDENTITY = Example(
    context=lambda: {
        "name": "Example Person",
        "url": "https://example.com/example",
        "tone": "good",
        "tip": "An example reason it is yours.",
        "picture": IDENTITY_STATES["states"].given()["picture"],
    }
)


IDENTITY_STATES = {
    # An account named as whose it is, in a card beside its band of counts, as
    # the Starred page draws your GitHub profile: its picture, its name with
    # a state beside it, its handle and a line in its own words. The card lays
    # the two out in a row, and the band, whose width is its frame's and never
    # its facts', still holds its four counts.
    "states": Example(
        '{% card label="Example profile" %}{% include "partials/_identity.html" with picture=picture '
        'name="Example Person" handle="@example" url="https://example.com/example" badge="Your account" '
        'tone="good" tip="An example reason it is yours." about="Runs an example homelab." only %}'
        '{% facts layout="band" label="Example account" %}'
        '{% fact "Example followers" value=12 url="https://example.com/followers" %}'
        '{% fact "Example following" value=9 url="https://example.com/following" %}'
        '{% fact "Example repositories" value=4 url="https://example.com/repositories" %}'
        '{% fact "Example starred" value=38 url="https://example.com/stars" %}'
        "{% endfacts %}{% endcard %}",
        {
            "picture": (
                "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 8 8'%3E"
                "%3Crect width='8' height='8' fill='%23888'/%3E%3C/svg%3E"
            )
        },
    ),
}


# You, the link and HQ: over the tailnet with every check confirmed and
# both ends leading to their machines, under Advanced HQ Posture.
CONNECTION_PEERING = Example(
    context=lambda: {"connection": _connection(), "posture": _posture((True, True))},
)


CONNECTION_PEERING_STATES = {
    # Which network the link rides over, as the panel's Connection row says
    # it: across the internet with the address to look up, through a relay,
    # and on the reader's own network.
    "carriage": Example(
        '{% section "Example link 7" %}{% facts %}'
        '{% fact "Connection" %}{% include "partials/_connection_peering.html#carriage" with peering=internet only %}'
        "{% endfact %}"
        '{% fact "Relayed" %}{% include "partials/_connection_peering.html#carriage" with peering=relay only %}'
        "{% endfact %}"
        '{% fact "Local" %}{% include "partials/_connection_peering.html#carriage" with peering=local only %}'
        "{% endfact %}{% endfacts %}{% endsection %}",
        lambda: {
            "internet": Peering("internet", "Direct, over the internet", address="203.0.113.7"),
            "relay": Peering("relay", "Relayed via DERP example-region"),
            "local": Peering("local", "Direct, on your network", address="192.0.2.30"),
        },
    ),
    # Off the tailnet with no reading of it: the address alone, and HQ by
    # the name it was asked for.
    "network": Example(
        '{% include "partials/_connection_peering.html" %}',
        lambda: {"connection": _connection(NETWORK_CHANNEL, path="", linked=False)},
    ),
    # Relayed, a check not yet read, neither end a known machine, and a
    # posture short of one check with another unread.
    "relayed": Example(
        '{% include "partials/_connection_peering.html" %}',
        lambda: {
            "connection": _connection(path="relayed", held=False, linked=False),
            "posture": _posture((True, False, None), advanced=False),
        },
    ),
    # Over the tailnet with a check that does not hold, drawn in the tone
    # the panel's status dot takes.
    "failing": Example(
        '{% include "partials/_connection_peering.html" %}',
        lambda: {"connection": _connection(failing=True)},
    ),
}


# Drawn only on a saved copy of HQ.
EXAMPLE_CONNECTION = Example(settings={"SEVERINO_RENDERED_COPY": True})


EXAMPLE_CONNECTION_STATES = {
    # On a saved copy of HQ, what a page says of the reader's connection is
    # said of the example visit: as a panel's notice, and as the short mark.
    "states": Example(
        '{% section "Example connection" %}{% include "partials/_example_connection.html" with notice=True %}'
        '{% facts %}{% fact "Example address" %}192.0.2.10 '
        '{% include "partials/_example_connection.html" %}{% endfact %}{% endfacts %}{% endsection %}',
        settings={"SEVERINO_RENDERED_COPY": True},
    ),
}


# A header HQ sends and a directive of its policy, each its value and its
# state as they stand before the browser has read the response.
RESPONSE_CHECK = Example(
    "{% for check in checks %}{% include part with check=check value=True %} "
    "{% include part with check=check value=False %}{% endfor %}",
    context=lambda: {
        "checks": RESPONSE_CHECKS[:1] + tuple(c for c in RESPONSE_CHECKS if c.directive)[:1],
        "part": "core/_response_check.html",
    },
)

"""A request in front of HQ, and the swept tailnet it arrived over."""

from django.test import RequestFactory
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.platform.core.tests.forwarding import arrived

from .people import someone

A_TAILNET_ADDRESS = "100.64.0.5"
A_LAN_ADDRESS = "10.0.0.50"


def a_tailnet(*devices):
    ProviderInventory.objects.update_or_create(
        kind="tailscale.device",
        defaults={"records": list(devices), "observed_at": timezone.now()},
    )


def a_device(name, address, *, user="", tags=(), observer=False, reach=(), **extra):
    """A swept device; a peer has a key and a completed handshake unless told otherwise."""

    return {
        "name": name,
        "addresses": [address],
        "user": user,
        "tags": list(tags),
        "self": observer,
        "reach": [
            {"port": port, "who": list(who), "rules": [{"who": list(who), "to": [f"{name}:{port}"], "line": 1}]}
            for port, who in reach
        ],
        "public_key": f"test-key-{name}",
        "last_handshake": timezone.now().isoformat(),
        **extra,
    }


def a_request(address=A_TAILNET_ADDRESS, *, host="hq.example.test", secure=True, **forwarded):
    """A request from ``address``, as the forwarded-header middleware hands it on."""

    request = RequestFactory().get(
        "/connection/", secure=secure, HTTP_HOST=host, **arrived(REMOTE_ADDR=address, **forwarded)
    )
    request.user = someone()
    return request

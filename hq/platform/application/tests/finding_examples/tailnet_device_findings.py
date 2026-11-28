from datetime import timedelta

from hq.platform.core.testing import declare

from ...tone import Tone
from . import Ago, Example, Examples, Raised

DEVICES = "tailscale.device"
LOCK = "host.tailnet_lock"
PHONE_KEY = "tlpub:" + "a1" * 32
SERVER_KEY = "tlpub:" + "b2" * 32
NOBODYS_KEY = "tlpub:" + "c3" * 32


def expiring_in(days: int) -> Ago:
    return Ago(-timedelta(days=days))


def devices(*records: dict, lock: dict | None = None, given=None, raises: tuple[Raised | str, ...] = ()) -> Example:
    readings = {DEVICES: tuple({"online": True, **record} for record in records)}
    if lock is not None:
        readings[LOCK] = ({"record": "lock", **lock},)
    return Example(readings=readings, given=given, raises=raises)


def a_ready_laptop() -> None:
    declare(
        "example-laptop",
        "machine",
        {"name": "example-laptop", "addresses": ["100.64.0.5"]},
        conditions=[{"type": "Ready", "status": True}],
        observed_generation=1,
    )


def a_declared_container() -> None:
    declare(
        "example-server-web",
        "portainer.container",
        {"connection_ref": "example-portainer", "host": "example-server", "name": "web"},
    )


def signers(*keys: str, enabled: bool = True, raises: tuple[Raised | str, ...] = ()) -> Example:
    """A phone and a server on the tailnet, each holding its lock key, the
    lock trusting ``keys``. The server runs a declared container."""

    return devices(
        {"name": "example-phone", "os": "iOS", "addresses": ["100.64.0.2"], "lock_key": PHONE_KEY},
        {"name": "example-server", "os": "linux", "addresses": ["100.64.0.3"], "lock_key": SERVER_KEY},
        lock={"enabled": enabled, "signers": [{"key": key, "votes": 1} for key in keys]},
        given=a_declared_container,
        raises=raises,
    )


# A device with nothing wrong.
SOUND = devices({"name": "example-host", "client_version": "1.80.0", "key_expires": ""})

EXAMPLES = (
    Examples(
        "tailnet-key-expiring",
        fires=(
            # Inside the window: its day and what to do.
            devices(
                {"name": "example-edge", "addresses": ["100.64.0.7"], "key_expires": expiring_in(30)},
                raises=("example-edge leaves the tailnet in 30\xa0days",),
            ),
            # Close enough to be serious rather than routine.
            devices(
                {"name": "example-edge", "key_expires": expiring_in(3)},
                raises=(Raised(title="example-edge leaves the tailnet in 3\xa0days", severity=Tone.SERIOUS),),
            ),
            # Already past: the machine has left.
            devices(
                {"name": "example-edge", "key_expires": expiring_in(-2)},
                raises=(Raised(title="example-edge has left the tailnet", severity=Tone.SERIOUS),),
            ),
        ),
        quiet=(
            SOUND,
            # Far away.
            devices({"name": "example-edge", "key_expires": expiring_in(120)}),
            # Expiry disabled has no date.
            devices({"name": "example-server", "key_expires": ""}),
        ),
    ),
    Examples(
        "tailnet-device-locked-out",
        fires=(
            # Raised though the device reading leaves it out.
            devices(
                {"name": "example-host"},
                lock={"enabled": True, "locked_out": ["example-locked"]},
                raises=("example-locked is locked out of the tailnet",),
            ),
        ),
        quiet=(SOUND, devices({"name": "example-host"}, lock={"enabled": True})),
    ),
    Examples(
        "tailnet-device-unapproved",
        fires=(
            # Waiting for approval, and where to approve it.
            devices(
                {"name": "example-host", "authorized": False, "addresses": ["100.64.0.8"]},
                {"name": "example-other"},
                raises=("example-host is waiting for tailnet approval",),
            ),
        ),
        quiet=(SOUND,),
    ),
    Examples(
        "tailnet-lock-unsigned",
        fires=(
            # Tailscale's words, and the command that lists keys.
            devices(
                {"name": "example-host", "lock_error": "node key not signed"},
                raises=("example-host is not signed for tailnet lock",),
            ),
        ),
        quiet=(SOUND,),
    ),
    Examples(
        "tailnet-lock-server-signs",
        fires=(
            # A signing server, and how to remove its key.
            signers(PHONE_KEY, SERVER_KEY, raises=("example-server can sign devices onto the tailnet",)),
        ),
        quiet=(
            # A signing phone.
            signers(PHONE_KEY),
            # A key no device holds is an unknown signer, not a server.
            signers(PHONE_KEY, NOBODYS_KEY),
            # With lock off, nothing signs.
            signers(SERVER_KEY, enabled=False),
        ),
    ),
    Examples(
        "tailnet-client-update",
        fires=(
            devices(
                {"name": "example-host", "update_available": True, "client_version": "1.102.3-t0abc"},
                {"name": "example-laptop", "client_version": "1.102.4-t0def"},
                raises=("Tailscale update available for example-host",),
            ),
            # A device the tailnet calls by its owner's label is the machine HQ lists under its own name.
            devices(
                {"name": "Someone’s Laptop", "addresses": ["100.64.0.5"], "update_available": True},
                given=a_ready_laptop,
                raises=("Tailscale update available for example-laptop",),
            ),
        ),
        quiet=(SOUND,),
    ),
    Examples(
        "tailnet-routes-unapproved",
        fires=(
            # Routes offered and not approved, with the approval on the machine's page.
            devices(
                {
                    "name": "example-router",
                    "advertised_routes": ["192.0.2.0/24", "198.51.100.0/24"],
                    "enabled_routes": ["198.51.100.0/24"],
                },
                raises=(
                    Raised(
                        title="example-router offers 1 unapproved route",
                        presses=("tailnet.routes.approve",),
                    ),
                ),
            ),
            # An exit node not approved cannot be used.
            devices(
                {
                    "name": "example-router",
                    "advertised_routes": ["0.0.0.0/0", "::/0"],
                    "offers_exit_node": True,
                    "exit_node_approved": False,
                },
                raises=(
                    Raised(
                        title="example-router offers 2 unapproved routes",
                        presses=("tailnet.routes.approve",),
                    ),
                ),
            ),
        ),
        quiet=(SOUND,),
    ),
)

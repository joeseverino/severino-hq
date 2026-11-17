"""The networks HQ judges an address by, declared once.

Kept under ``hq.config`` because the settings read them before any app loads,
and the application reads the same names. The tailnet's ranges reach the
controller through the bridge contract (the tailscale adapter's ``SHARED``).
"""

from ipaddress import ip_address

# Tailscale numbers a tailnet's devices from the carrier-grade NAT range and
# one IPv6 ULA prefix. Nothing else on a normal network uses either, so an
# address in them is reachable by tailnet members and by nobody else.
TAILNET_RANGES = ("100.64.0.0/10", "fd7a:115c:a1e0::/48")


def is_loopback(host: str) -> bool:
    """Whether ``host`` (a name, an address, or one in brackets) means this machine."""

    name = host.strip().lower().removeprefix("[").removesuffix("]")
    if name == "localhost":
        return True
    try:
        return ip_address(name).is_loopback
    except ValueError:
        return False

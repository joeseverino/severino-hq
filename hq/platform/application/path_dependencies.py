"""What changing or losing each hop on a service's path would mean."""

from .path_model import Hop

# What changing or losing a hop does to the name, by step.
_CONSEQUENCES = {
    "dns": "The name stops resolving, or resolves somewhere else.",
    "edge": "If it is switched off, visitors reach the origin directly and Cloudflare's certificate no longer applies.",
    "redirect": "Visitors stop being sent on.",
    "served": "The site stops answering at this name.",
    "ingress": "If it is removed, the name stops reaching the app.",
    "machine": "If it is down, this service is down.",
    # The address a record answers with: move it and the record still points
    # at the old one.
    "network": "If the machine's address changes, this name stops working.",
    "upstream": "If nothing listens there, you get 502 Bad Gateway.",
    "container": "The service stops answering.",
    "hq": "HQ stops answering at this name.",
}


def consequence_of(hop: Hop) -> str:
    """What changing this hop would do, or "" where nothing depends on it."""

    return _CONSEQUENCES.get(hop.step, "")

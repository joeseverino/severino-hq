"""Readings from the tailnet credential."""

from __future__ import annotations

from .contract import ObservationRecord, ObservationSpec


def _dns_title(record) -> str:
    """The resolvers and what the tailnet does with them."""

    parts = [", ".join(record.get("nameservers") or ()) or "No global resolvers"]
    if record.get("magic_dns"):
        parts.append("MagicDNS on")
    if record.get("override_local_dns"):
        parts.append("overrides local DNS")
    return " · ".join(parts)


class TailnetDnsRecord(ObservationRecord):
    record: str
    # Global resolvers, by address.
    nameservers: tuple[str, ...] = ()
    # Whether the global resolvers replace each device's own.
    override_local_dns: bool = False
    magic_dns: bool = False
    search_paths: tuple[str, ...] = ()
    # Domain to the resolvers that answer for it.
    split_dns: dict[str, tuple[str, ...]] = {}


class TailnetSettingsRecord(ObservationRecord):
    record: str
    devices_approval_on: bool | None = None
    devices_key_duration_days: int | None = None
    devices_auto_updates_on: bool | None = None
    users_approval_on: bool | None = None
    network_flow_logging_on: bool | None = None
    regional_routing_on: bool | None = None
    posture_identity_collection_on: bool | None = None
    https_enabled: bool | None = None
    acls_externally_managed_on: bool | None = None
    # Settings the credential could not see, and the scope each needs.
    unread: str = ""


# Personal data, stored for the estate view: who holds access to the tailnet
# and in what role.
class TailnetUserRecord(ObservationRecord):
    id: str
    display_name: str = ""
    login_name: str = ""
    role: str = ""
    status: str = ""
    created: str = ""
    last_seen: str = ""


def _resolvers(record) -> tuple[str, ...]:
    found = list(record.get("nameservers") or ())
    for resolvers in (record.get("split_dns") or {}).values():
        found.extend(resolvers or ())
    return tuple(dict.fromkeys(str(address) for address in found))


OBSERVATIONS: tuple[ObservationSpec, ...] = (
    ObservationSpec(
        "tailscale.dns",
        "tailscale",
        "Tailnet DNS",
        TailnetDnsRecord,
        requires=("dns:read",),
        addresses=_resolvers,
        title=_dns_title,
        relation="Tailnet DNS server",
    ),
    ObservationSpec(
        "tailscale.settings",
        "tailscale",
        "Tailnet settings",
        TailnetSettingsRecord,
        requires=("feature_settings:read",),
        title=lambda record: "Tailnet settings",
        relation="Governed by",
    ),
    ObservationSpec(
        "tailscale.user",
        "tailscale",
        "Tailnet user",
        TailnetUserRecord,
        requires=("users:read",),
        title=lambda record: str(
            record.get("display_name") or record.get("login_name") or ""
        ),
        relation="Tailnet member",
    ),
)

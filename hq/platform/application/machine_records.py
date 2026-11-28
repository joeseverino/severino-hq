"""The shapes the machines resource answers in: each machine's row and record."""

from typing import Annotated, TypedDict

from .answer_fields import DateTime, ObservedAt, PageUrl
from .describe import Describe, listed
from .record_parts import ComposeProject
from .tone import Tone
from .ui import ended


class RolePart(TypedDict):
    id: Annotated[str, Describe("The role's stable id, such as `tailnet-dns`.")]
    label: Annotated[str, Describe("The role, in words.")]
    detail: Annotated[str, Describe("What the role means, and what was read that shows it.")]


class MachineRow(TypedDict):
    name: Annotated[str, Describe("The machine's name, which a read of one names it by.")]
    url: PageUrl
    state: Annotated[
        str,
        Describe(
            "Whether it can be reached now, such as `online`, `away` for a personal device that is off, "
            "`offline` or `not monitored`."
        ),
    ]
    tone: Annotated[
        str, Describe(ended(f"How its state is colored: {listed((Tone.GOOD, Tone.NEUTRAL, Tone.SERIOUS))}"))
    ]
    role: Annotated[
        str, Describe("The role HQ's record of the machine gives it, such as `Docker host`; empty for none.")
    ]
    roles: Annotated[
        list[RolePart], Describe("What the machine does for the rest of the network, as derived from what was read.")
    ]
    addresses: Annotated[
        list[str], Describe("Every address HQ knows reaches the machine, with a port where one is part of it.")
    ]
    hostnames: Annotated[list[str], Describe("The hostnames served from the machine.")]
    runs_hq: Annotated[bool, Describe("Whether HQ itself runs on the machine.")]


class HeaderAddressPart(TypedDict):
    label: Annotated[str, Describe("Which network the address is on, such as `Tailnet`, `LAN` or `Public`.")]
    address: Annotated[str, Describe("The address you reach the machine at there.")]
    holder: Annotated[
        str, Describe("Who holds a public address, such as the company that hosts it; empty for any other.")
    ]


class MachineContainerPart(TypedDict):
    name: Annotated[str, Describe("The container's name, as Docker reports it.")]
    stack: ComposeProject
    image: Annotated[str, Describe("The image reference it runs.")]
    state: Annotated[str, Describe("Docker's state for the container, such as `running`.")]
    status: Annotated[str, Describe("Docker's status line for the container.")]
    published: Annotated[str, Describe("The ports it publishes, as Docker lists them; empty for none.")]
    watcher: Annotated[str, Describe("The key of HQ's record that watches it; empty for none.")]
    observed_at: ObservedAt | None


class MetricPart(TypedDict):
    label: Annotated[str, Describe("What it measures, such as `CPU`.")]
    value: Annotated[str, Describe("The measure, as a page shows it, such as `12%`.")]
    detail: Annotated[str, Describe("A line under it, such as `4 cores`.")]


class LoadPart(TypedDict):
    metrics: Annotated[
        list[MetricPart],
        Describe("Each measure of the last reading, less a count of containers where `containers` lists them."),
    ]
    observed_at: ObservedAt | None


class ArrivalPart(TypedDict):
    last_seen: Annotated[str, DateTime(), Describe("When a request last came to HQ from it, an ISO 8601 date-time.")]
    how: Annotated[str, Describe("How that request came, such as `directly over the tailnet`.")]


class PeeringPart(TypedDict):
    last_handshake: Annotated[str, Describe("When it and HQ's machine last completed a handshake, as reported.")]
    active: Annotated[bool, Describe("Whether traffic is passing between the two now.")]
    path: Annotated[
        str, Describe("How the two reach each other: `direct`, `relayed`, or `negotiating` before any traffic.")
    ]
    direct_endpoint: Annotated[str, Describe("The address a direct path reaches it at; empty for none.")]
    relay: Annotated[str, Describe("The relay that carries a path with no direct one, or stands by for one.")]
    rx_bytes: Annotated[int, Describe("The bytes HQ's machine has received from it.")]
    tx_bytes: Annotated[int, Describe("The bytes HQ's machine has sent it.")]
    public_key: Annotated[str, Describe("Its node key.")]


class OpeningPart(TypedDict):
    port: Annotated[int, Describe("A port the tailnet policy admits a device to on it.")]
    open_to: Annotated[
        list[str], Describe("Who the policy lets in at that port: a machine by its name, a user or tag as written.")
    ]
    serves: Annotated[str, Describe("What serves it there, by its own name; empty when HQ knows of nothing.")]
    bound_at: Annotated[list[str], Describe("The addresses it is bound at there, as HQ read them; empty when unknown.")]
    open: Annotated[bool, Describe("Whether it is bound beyond loopback, or may be, so the policy's word reaches it.")]


class PresencePart(TypedDict):
    online: Annotated[bool, Describe("Whether the tailnet says it is online now.")]
    last_seen: Annotated[str | None, Describe("When the tailnet last saw it, as reported; null when not said.")]
    tailnet_name: Annotated[str, Describe("What the tailnet calls it.")]
    dns_name: Annotated[str, Describe("Its MagicDNS name.")]
    tailnet_address: Annotated[str, Describe("Its address on the tailnet.")]
    lan_address: Annotated[str, Describe("The LAN address its client reports; empty for none.")]
    os: Annotated[str, Describe("The operating system its client reports.")]
    client_version: Annotated[str, Describe("The version of the tailnet client it runs.")]
    update_available: Annotated[bool, Describe("Whether a newer client is published for it.")]
    key_expires: Annotated[str | None, Describe("When its node key runs out, as reported; null when it does not.")]
    authorized: Annotated[bool, Describe("Whether the tailnet has authorized it; one it has not reaches nothing.")]
    tags: Annotated[list[str], Describe("The tags it carries.")]
    advertised_routes: Annotated[list[str], Describe("The routes it offers the tailnet.")]
    enabled_routes: Annotated[list[str], Describe("Those of its routes the tailnet has approved, which alone work.")]
    offers_exit_node: Annotated[bool, Describe("Whether it offers to be an exit node.")]
    exit_node_approved: Annotated[bool, Describe("Whether the tailnet has approved it as an exit node.")]
    ssh_enabled: Annotated[bool, Describe("Whether Tailscale SSH is on for it.")]
    blocks_incoming: Annotated[bool, Describe("Whether it accepts no inbound connection at all.")]
    external: Annotated[bool, Describe("Whether it belongs to another tailnet, shared into this one.")]
    lock_error: Annotated[str, Describe("Why tailnet lock has not signed it; empty when signed or lock is off.")]
    signs_tailnet_lock: Annotated[bool, Describe("Whether its device is one tailnet lock trusts to sign.")]
    unapproved_routes: Annotated[
        list[str], Describe("Those of its advertised routes the tailnet has not approved, which reach nothing.")
    ]
    observer: Annotated[bool, Describe("Whether it is HQ's own machine, whose tailnet client this reading is from.")]
    reached_hq: Annotated[
        ArrivalPart | None, Describe("The last request that came to HQ from it; null when none came lately.")
    ]
    peering: Annotated[
        PeeringPart | None, Describe("How it and HQ's machine are talking; null until a handshake completes.")
    ]
    openings: Annotated[list[OpeningPart], Describe("Who the tailnet policy lets in, per port.")]
    observed_at: ObservedAt | None


class MachineRecord(MachineRow):
    """One machine whole: how it is reached, what it serves and what runs on it."""

    node: Annotated[str, Describe("The machine's id on the map: `machine:` and its name.")]
    declaration: Annotated[str, Describe("The key of HQ's record of the machine; empty when HQ keeps none.")]
    aliases: Annotated[list[str], Describe("Other names the same machine is known by.")]
    address: Annotated[str, Describe("The address its connection reaches it at; empty when none says.")]
    tailnet_device: Annotated[
        str, Describe("The key of HQ's record of its tailnet device, which approves its routes; empty for none.")
    ]
    header_addresses: Annotated[
        list[HeaderAddressPart], Describe("The addresses you reach it at, tailnet then LAN then public.")
    ]
    containers_visible: Annotated[
        bool,
        Describe("Whether any connected credential reads containers, so an empty `containers` means none run there."),
    ]
    hq_hostnames: Annotated[list[str], Describe("The hostnames HQ answers at on the machine; empty unless `runs_hq`.")]
    reached_by: Annotated[list[str], Describe("The connections HQ reaches it through, by ref.")]
    opened_by: Annotated[
        list[str], Describe("Those of `reached_by` whose credential opens a shell or a Docker environment on it.")
    ]
    unanswered: Annotated[list[str], Describe("Those of `reached_by` that did not answer their last check.")]
    resources: Annotated[list[str], Describe("The keys of HQ's records on the machine.")]
    containers: Annotated[list[MachineContainerPart], Describe("The containers last read on it.")]
    running: Annotated[int, Describe("How many of `containers` are running.")]
    load: Annotated[LoadPart | None, Describe("Its last reading of load; null when nothing reads it.")]
    presence: Annotated[
        PresencePart | None, Describe("What the tailnet reports of it; null when the tailnet does not know it.")
    ]

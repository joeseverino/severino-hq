"""Tailnet devices and the tailnet policy: settings HQ keeps, not things it made."""

import json
from collections.abc import Mapping
from typing import Annotated, Any

from django.utils.text import capfirst
from pydantic import Field

from hq.config.networks import TAILNET_RANGES
from hq.platform.application.command_fields import RequiredText
from hq.platform.application.describe import Describe
from hq.platform.application.ui import ended

from ..connection_shapes import OAUTH_CLIENT, BlankOrConnectionRef
from ..consoles import tailscale_machine
from ..observations import tailscale as readings
from ..observations.contract import ReadingPart
from ..permissions import Tailscale
from ..provider_spec import (
    ConnectionKind,
    ControllerVerification,
    InboundSpec,
    Minter,
    ProviderModel,
    ProviderSpec,
    SharedValue,
    SignatureScheme,
    applies,
    expiry_phrase,
)

TAILNET_KIND = "tailscale.device"
TAILNET_POLICY_KIND = "tailscale.policy"


class TailnetDeviceSpec(ProviderModel):
    """A machine on the tailnet whose settings HQ keeps, not one it created.

    Like a watched container, it joined by somebody running `tailscale up` on
    it. HQ holds the few decisions about it that are made once, then forgotten,
    and have no symptom until the day they matter.
    """

    # Optional, like the policy's: a device is adopted from a reading the
    # daemon gave for free, which names no credential, and the reconciler
    # resolves the single Tailscale connection when this is blank. Required, it
    # made every device fail adoption on a field the record could never carry.
    connection_ref: BlankOrConnectionRef = Field(
        default="",
        title="Tailscale",
        description="The connection HQ uses to change this device.",
    )
    # Named as the tailnet names it, often not what HQ calls the machine: the
    # two join on the address they share, and HQ's name here would leave the
    # controller guessing which device was meant.
    name: RequiredText = Field(
        max_length=200,
        title="Device",
        description="The device name, as the tailnet reports it.",
    )
    key_expiry_disabled: bool = Field(
        default=False,
        title="Disable key expiry",
        description=("Otherwise the device goes offline when its key expires."),
    )


class TailnetPolicySpec(ProviderModel):
    """The tailnet's access policy, as HQ last read it.

    Not something an operator adds here: it exists because a tailnet does.
    ``created_from`` keeps it out of the "add a resource" picker for that
    reason: there is exactly one, and it arrived with the credential.
    """

    connection_ref: Annotated[
        BlankOrConnectionRef, Describe("", api="The Tailscale connection the policy is read through.")
    ] = Field(default="", title="Tailscale")
    document: str = Field(
        default="",
        title="Policy",
        # What saving does is the provider's declared change effect, which the
        # form states under the field; saying it here too would print it twice.
        description="The tailnet's access policy.",
        # Tailscale writes its policy file as JSON with comments.
        json_schema_extra={"contentMediaType": "application/hujson"},
    )


def _tailnet_device_identity(spec: dict[str, Any]) -> tuple[str, ...]:
    name = str(spec.get("name", ""))
    return (name,) if name else ()


# What a tailnet device declaration is *about* its machine, used to qualify its
# key. Not a service facet: a device is not something a hostname is served by,
# so it belongs here rather than in the facet the service composition reads.
TAILNET_FACET = "tailnet"


def _tailnet_device_key_hint(spec: dict[str, Any]) -> str:
    """``<name>-tailnet``, because a machine already answers to ``<name>``.

    Keys are unique across kinds; an aspect of something is keyed by its name
    and the aspect, as ``<name>-dns`` and ``<name>-proxy`` are.
    """
    name = str(spec.get("name", "")).strip()
    return f"{name}-{TAILNET_FACET}" if name else ""


def _tailnet_device_readout(spec: dict[str, Any], status: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
    """What HQ asked for about this device, beside what the tailnet reports."""

    wanted = "Never expires" if spec.get("key_expiry_disabled") else "Expires"
    observed = ""
    if status:
        observed = (
            "Never expires" if status.get("key_expiry_disabled") else expiry_phrase(str(status.get("key_expires", "")))
        )
    return (
        ("Device", "", str(spec.get("name", ""))),
        ("Connection", "", str(spec.get("connection_ref", ""))),
        ("Node key", wanted, observed),
    )


def _tailnet_device_from_record(record: dict[str, Any]) -> dict[str, Any]:
    """A tailnet device, as the declaration that would reproduce it.

    Key expiry is read back, since a quietly wrong expiry is what the reading
    exists to catch: a device adopted with its name alone would assert a
    ``key_expiry_disabled`` no sweep confirmed. No expiry is the setting, not an
    unknown date, the same reading a reconcile makes.
    """

    return {
        "name": record.get("name", ""),
        "key_expiry_disabled": not record.get("key_expires"),
    }


DEVICE = ProviderSpec(
    TAILNET_KIND,
    "Settings HQ keeps for one device on your tailnet, such as key expiry.",
    TailnetDeviceSpec,
    actions={
        "reconcile": applies(
            automatic=True,
            verification=ControllerVerification(
                timeout_seconds=60,
                interval_seconds=10,
            ),
        ),
        "approve-routes": applies(),
    },
    label="Tailnet device",
    connection_providers=("tailscale",),
    console=tailscale_machine,
    readout=_tailnet_device_readout,
    _from_record=_tailnet_device_from_record,
    # Carries an expiry, so the round-trip guard has something to check
    # rather than passing on a field the fixture never supplied.
    sample_record={
        "name": "example-device",
        "key_expires": "2026-12-01T00:00:00Z",
        "document": "",
    },
    identity=_tailnet_device_identity,
    key_hint=_tailnet_device_key_hint,
    choices="hq.platform.application.provider_choices:tailnet_device",
    # HQ's own bookkeeping: Tailscale does not hold it and no device
    # reading echoes it back, so a declaration setting it would assert a
    # field nothing can confirm.
    unobservable_fields=("connection_ref",),
    # HQ did not create the device and cannot delete it: running
    # `tailscale up` on the machine is what put it there. Removal means HQ
    # stops keeping its settings, as for a watched container. Left False,
    # removal queues a delete this provider has no action for and is
    # refused, leaving the declaration impossible to remove.
    declaration_only=True,
    change_effects=(
        (
            "key_expiry_disabled",
            "Turning it off restores key expiry.",
        ),
    ),
    removal_note=lambda spec: (
        f"HQ stops managing {ended(spec.get('name', 'this device'))} Its current settings stay as they are."
    ),
)


def _policy_readout(
    spec: Mapping[str, Any] | None, status: Mapping[str, Any] | None
) -> tuple[tuple[str, str, str], ...]:
    """How many grants, groups and tests the policy holds, counted from the
    document itself: what was last read when there is one, else what is asked.

    The status stores the document as text, not its parts, so there is no
    ``status["grants"]`` to count. A policy written as ``acls`` counts those as
    its grants.
    """

    text = str((status or {}).get("document") or (spec or {}).get("document") or "")
    try:
        parsed = json.loads(text) if text else None
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        return (("Grants", "", ""), ("Groups", "", ""), ("Tests", "", ""))
    grants = [*(parsed.get("grants") or ()), *(parsed.get("acls") or ())]
    return (
        ("Grants", "", str(len(grants))),
        ("Groups", "", str(len(parsed.get("groups") or {}))),
        ("Tests", "", str(len(parsed.get("tests") or ()))),
    )


def _policy(spec: Mapping[str, Any]) -> dict[str, Any] | None:
    """A declaration's policy document, parsed; None where it is not JSON (a
    document with comments), which the change's own line diff then shows."""

    text = str(spec.get("document") or "")
    try:
        parsed = json.loads(text) if text.strip() else {}
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _names(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _access(policy: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """Each (source, destination, port) the policy admits, as its grants and
    its older ``acls`` write them, in the document's order."""

    found: list[tuple[str, str, str]] = []
    for grant in policy.get("grants") or ():
        if isinstance(grant, Mapping):
            found += [
                (src, dst, port)
                for src in _names(grant.get("src"))
                for dst in _names(grant.get("dst"))
                for port in _names(grant.get("ip"))
            ]
    for acl in policy.get("acls") or ():
        if isinstance(acl, Mapping) and acl.get("action", "accept") == "accept":
            for src in _names(acl.get("src") or acl.get("users")):
                for target in _names(acl.get("dst") or acl.get("ports")):
                    dst, _, port = target.rpartition(":")
                    found.append((src, dst or target, port if dst else "*"))
    return found


def _deny_tests(policy: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """Each (source, protocol, destination) a test of the policy says is denied."""

    return [
        (str(test.get("src", "")), str(test.get("proto") or ""), dst)
        for test in policy.get("tests") or ()
        if isinstance(test, Mapping)
        for dst in _names(test.get("deny"))
    ]


def _policy_change_words(before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
    """What a policy change opens and which deny tests it drops ("Opens
    dell:22 to mac; drops the test that mac is denied dell:22"), or "" when
    it does neither."""

    was, now = _policy(before), _policy(after)
    if was is None or now is None:
        return ""
    clauses: list[str] = []
    held = set(_access(was))
    opened: dict[str, dict[str, list[str]]] = {}
    for src, dst, port in _access(now):
        ports = opened.setdefault(src, {}).setdefault(dst, [])
        if (src, dst, port) not in held and port not in ports:
            ports.append(port)
    # Sources opened the same places are said together.
    by_reach: dict[str, list[str]] = {}
    for src, places in opened.items():
        reach = ", ".join(f"{dst}:{', '.join(ports)}" for dst, ports in places.items() if ports)
        if reach:
            by_reach.setdefault(reach, []).append(src)
    clauses += [f"opens {reach} to {', '.join(sources)}" for reach, sources in by_reach.items()]
    kept = set(_deny_tests(now))
    dropped: dict[tuple[str, str], list[str]] = {}
    for src, proto, dst in _deny_tests(was):
        if (src, proto, dst) not in kept and dst not in dropped.get((src, proto), []):
            dropped.setdefault((src, proto), []).append(dst)
    clauses += [
        f"drops the test that {src} is denied {', '.join(dsts)}{f' over {proto}' if proto else ''}"
        for (src, proto), dsts in dropped.items()
    ]
    said = "; ".join(clauses)
    return capfirst(said)


POLICY = ProviderSpec(
    TAILNET_POLICY_KIND,
    "The tailnet's access policy. HQ runs the policy's tests before applying a change.",
    TailnetPolicySpec,
    actions={
        "reconcile": applies(
            verification=ControllerVerification(
                timeout_seconds=60,
                interval_seconds=10,
            )
        ),
    },
    label="Tailnet policy",
    connection_providers=("tailscale",),
    hostnames=None,
    # There is one, it came with the tailnet, and nobody adds a second.
    created_from="tailnet",
    # Adopted from the sweep, so the declaration starts byte-identical to
    # the live policy and editing it is editing what is actually there.
    _from_record=lambda record: {"document": record.get("document", "")},
    sample_record={"document": ""},
    identity=lambda _spec: ("tailnet",),
    key_hint=lambda _spec: "tailnet-policy",
    name=lambda _spec: "Tailnet policy",
    # As for a device: a policy reading returns the document, not how HQ
    # fetched it.
    unobservable_fields=("connection_ref",),
    # One policy per tailnet, which HQ did not create and cannot delete.
    # Removal means HQ stops keeping it, as for a device.
    declaration_only=True,
    # The most valuable single control in the estate: it decides who can
    # reach what, everywhere, and a mistake in it is not confined to one
    # service. A credential that can change this can open the network, so
    # changing it asks a person first. That person is the check on what a
    # change opens, so the change is headed by what it opens and which deny
    # tests it drops.
    requires_approval=True,
    change_words=_policy_change_words,
    change_effects=(
        (
            "document",
            "Applied only if the policy's own tests pass.",
        ),
    ),
    readout=_policy_readout,
    parts=(
        ReadingPart("settings", "Tailnet settings", (Tailscale.FEATURE_SETTINGS_READ,)),
        ReadingPart("dns", "Tailnet DNS", (Tailscale.DNS_READ,)),
        ReadingPart("services", "Tailnet services", (Tailscale.SERVICES_READ,)),
    ),
)

SHARED = (
    SharedValue(
        "TailnetRange",
        TAILNET_RANGES,
        "The address ranges Tailscale numbers a tailnet's devices from.",
        varnames=("TailnetRangeIPv4", "TailnetRangeIPv6"),
    ),
    SharedValue(
        "TailscaleScope",
        tuple(Tailscale),
        "A read scope a Tailscale credential is granted, which a refused read names.",
        varnames=tuple("TailscaleScope" + scope.name.title().replace("_", "") for scope in Tailscale),
    ),
)

# Declarations only: the controller half is still the core's.
DEFINITIONS = (DEVICE, POLICY)
OBSERVATIONS = readings.OBSERVATIONS

# The connection this provider's credential arrives through, beside its kinds:
# admitting the module admits both.
CONNECTIONS = {
    "tailscale": ConnectionKind(
        "Tailscale",
        "scoped",
        OAUTH_CLIENT,
        create="An OAuth client in the Tailscale admin console, under Settings, Trust credentials.",
        create_url="https://tailscale.com/kb/1215/oauth-clients",
        gives="Your tailnet's devices, who may reach what, and its DNS.",
        minter=Minter(
            task="mint:tailscale",
            stores=(("--store", "CLIENT_SECRET"), ("--store-id", "CLIENT_ID")),
            bootstrap=(
                ("TAILSCALE_BOOTSTRAP_CLIENT_ID", "CLIENT_ID"),
                ("TAILSCALE_BOOTSTRAP_CLIENT_SECRET", "CLIENT_SECRET"),
            ),
        ),
    )
}

# What the tailnet's webhook sends, each event making due the reading of what
# it changed; Tailscale's docs name each event and how it signs a delivery
# (https://tailscale.com/kb/1213/webhooks). The user events that page leaves
# out, which the console offers, are named in Tailscale's client
# (https://github.com/tailscale/tailscale-client-go-v2/blob/main/webhooks.go).
# Its deprecated nodeAuthorized and nodeNeedsAuthorization are left out, as
# nodeApproved and nodeNeedsApproval replace them.
_DEVICE_EVENTS = (
    "nodeCreated",
    "nodeNeedsApproval",
    "nodeApproved",
    "nodeKeyExpiringInOneDay",
    "nodeKeyExpired",
    "nodeDeleted",
    "nodeNeedsSignature",
    "nodeSigned",
    "exitNodeIPForwardingNotEnabled",
    "subnetIPForwardingNotEnabled",
)
_USER_EVENTS = (
    "userCreated",
    "userNeedsApproval",
    "userApproved",
    "userRoleUpdated",
    "userSuspended",
    "userRestored",
    "userDeleted",
)
INBOUND = {
    "tailscale": InboundSpec(
        {
            **dict.fromkeys(_DEVICE_EVENTS, (TAILNET_KIND,)),
            **dict.fromkeys(_USER_EVENTS, (readings.USER_KIND,)),
            "policyUpdate": (TAILNET_POLICY_KIND,),
        },
        path="/tailscale",
        # "t=<unix time>,v1=<hex>", an HMAC-SHA256 of "<t>.<body>", refused
        # when signed more than five minutes ago.
        scheme=SignatureScheme("Tailscale-Webhook-Signature", "v1", timestamp="t", window_seconds=300),
        # "The root payload object is always an array of events", and a
        # delivery carries no ID of its own.
        batched=True,
        event_pointer="/type",
        subject_pointer="/tailnet",
        secret_binding="TAILSCALE_WEBHOOK_SECRET",  # noqa: S106 - the Worker secret's name, never its value
        # Tailscale states no cap; an event is under a kilobyte, so this
        # holds a thousand in one delivery.
        max_body_bytes=1024 * 1024,
        # Every change to the policy file is an event. A device goes online
        # and offline with none.
        complete=(TAILNET_POLICY_KIND,),
    ),
}

"""Tailnet devices and the tailnet policy: settings HQ keeps, not things it made."""

from __future__ import annotations

import json

from collections.abc import Mapping
from typing import Any

from pydantic import Field

from ..consoles import tailscale_machine
from ..observations.contract import ReadingPart
from ..connection_shapes import OAUTH_CLIENT
from ..provider_spec import (
    ConnectionKind,
    ControllerVerification,
    ProviderModel,
    ProviderSpec,
    applies,
    expiry_phrase,
)


TAILNET_KIND = "tailscale.device"
TAILNET_POLICY_KIND = "tailscale.policy"


class TailnetDeviceSpec(ProviderModel):
    """A machine on the tailnet whose settings HQ keeps, not one it created.

    The same shape as a watched container: the device joined the tailnet by
    somebody running `tailscale up` on it, and HQ has no business pretending
    otherwise. What it can hold is the handful of decisions about that device
    which are made once and then quietly forgotten, and which have no symptom
    until the day they matter.

    Named as the tailnet names it. That is often not what HQ calls the machine,
    and the join between the two is the address they share; using HQ's name here
    would mean the controller had to guess which device was meant.
    """

    # Optional, like the policy's: a device is adopted from a reading the
    # daemon gave for free, which names no credential, and the reconciler
    # resolves the single Tailscale connection when this is blank. Required, it
    # made every device fail adoption on a field the record could never carry.
    connection_ref: str = Field(
        default="",
        max_length=160,
        title="Tailscale",
        description="The connection HQ uses to change this device.",
    )
    name: str = Field(
        min_length=1,
        max_length=200,
        title="Device",
        description="The device name, as the tailnet reports it.",
    )
    key_expiry_disabled: bool = Field(
        default=False,
        title="Disable key expiry",
        description=(
            "Disables node key expiry. Without it, the device becomes "
            "unreachable when its key expires."
        ),
    )


class TailnetPolicySpec(ProviderModel):
    """The tailnet's access policy, as HQ last read it.

    Not something an operator adds here: it exists because a tailnet does.
    ``created_from`` keeps it out of the "add a resource" picker for that
    reason: there is exactly one, and it arrived with the credential.
    """

    connection_ref: str = Field(default="", max_length=160, title="Tailscale")
    document: str = Field(
        default="",
        title="Policy",
        # What saving does is the provider's declared change effect, which the
        # form states under the field; saying it here too would print it twice.
        description="The tailnet's access policy.",
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


def _tailnet_device_readout(
    spec: dict[str, Any], status: dict[str, Any]
) -> tuple[tuple[str, str, str], ...]:
    """What HQ asked for about this device, beside what the tailnet reports."""

    wanted = "Never expires" if spec.get("key_expiry_disabled") else "Expires"
    observed = ""
    if status:
        observed = (
            "Never expires"
            if status.get("key_expiry_disabled")
            else expiry_phrase(str(status.get("key_expires", "")))
        )
    return (
        ("Device", "", str(spec.get("name", ""))),
        ("Connection", "", str(spec.get("connection_ref", ""))),
        ("Node key", wanted, observed),
    )


def _tailnet_device_from_record(record: dict[str, Any]) -> dict[str, Any]:
    """A tailnet device, as the declaration that would reproduce it.

    Key expiry is read back, not dropped. The daemon reading is described as
    holding "presence and key expiry, which are the two that go wrong quietly",
    and this mapping kept only the name, so every device asserted a
    ``key_expiry_disabled`` no sweep ever confirmed, which is the quiet way it
    goes wrong. Absence of an expiry is the setting rather than an unknown
    date, the same reading a reconcile makes.
    """

    return {
        "name": record.get("name", ""),
        "key_expiry_disabled": not record.get("key_expires"),
    }


DEVICE = ProviderSpec(
    TAILNET_KIND,
    "Settings HQ keeps for one device on your tailnet, such as key "
    "expiry.",
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
    from_record=_tailnet_device_from_record,
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
            "Applied on the next reconcile. Turning it off restores key "
            "expiry.",
        ),
    ),
    removal_note=lambda spec: (
        f"HQ stops managing {spec.get('name', 'this device')}. Its current "
        "settings stay as they are."
    ),
)

def _policy_readout(spec: Mapping[str, Any] | None, status: Mapping[str, Any] | None) -> tuple[tuple[str, str, str], ...]:
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


POLICY = ProviderSpec(
    TAILNET_POLICY_KIND,
    "The tailnet's access policy. HQ runs the policy's tests before "
    "applying a change.",
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
    from_record=lambda record: {"document": record.get("document", "")},
    sample_record={"document": ""},
    identity=lambda spec: ("tailnet",),
    key_hint=lambda spec: "tailnet-policy",
    name=lambda spec: "Tailnet policy",
    # As for a device: a policy reading returns the document, not how HQ
    # fetched it.
    unobservable_fields=("connection_ref",),
    # One policy per tailnet, which HQ did not create and cannot delete.
    # Removal means HQ stops keeping it, as for a device.
    declaration_only=True,
    # The most valuable single control in the estate: it decides who can
    # reach what, everywhere, and a mistake in it is not confined to one
    # service. A credential that can change this can open the network, so
    # changing it asks a person first.
    requires_approval=True,
    change_effects=(
        (
            "document",
            "Saving records it. Reconciling applies it if the policy's own "
            "tests pass.",
        ),
    ),
    readout=_policy_readout,
    parts=(
        ReadingPart("settings", "Tailnet settings", ("feature_settings:read",)),
        ReadingPart("dns", "Tailnet DNS", ("dns:read",)),
        ReadingPart("services", "Tailnet services", ("services:read",)),
    ),
)

# Declarations only: the controller half is still the core's.
DEFINITIONS = (DEVICE, POLICY)

# The connection this provider's credential arrives through, beside its kinds:
# admitting the module admits both.
CONNECTIONS = {"tailscale": ConnectionKind("Tailscale", "scoped", OAUTH_CLIENT)}

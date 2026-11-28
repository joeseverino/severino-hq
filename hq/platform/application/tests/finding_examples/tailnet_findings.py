import json

from hq.platform.core.testing import connection, declare

from ...finding_steps import Do
from . import Example, Examples, Raised

POLICY = "tailscale.policy"
LOCK = "host.tailnet_lock"
DEVICES = "tailscale.device"
DNS = "tailscale.dns"
# The tailnet's IPv4 and IPv6 ranges (``reach.TAILNET``).
TAILNET_V4 = "100.64.0.0/10"
TAILNET_V6 = "fd7a:115c:a1e0::/48"


def policy(*, lock: dict | None = None, devices: tuple[dict, ...] = (), **record) -> dict:
    readings = {POLICY: ({"record": "policy", **record},)}
    if lock is not None:
        readings[LOCK] = ({"record": "lock", **lock},)
    if devices:
        readings[DEVICES] = devices
    return readings


def granted(*tags: str) -> dict:
    """Tags a grant names, beside a device that wears ``tag:worn``."""

    return policy(
        grants=[{"src": ["autogroup:member"], "dst": [f"{tag}:443" for tag in tags], "ip": ["tcp:443"]}],
        devices=({"name": "example-box", "tags": ["tag:worn"]},),
    )


GRANT = {"src": ["example-mac"], "dst": ["example-host"], "ip": ["tcp:443", "tcp:8000"]}
TEST = {"src": "example-mac", "proto": "tcp", "accept": ["example-host:443", "example-host:8000"]}
PROXY = ("example-proxy", {"port_bindings": [{"container_port": "443/tcp", "host_ip": "", "host_port": "443"}]})


def a_machine() -> None:
    declare("example-host", "machine", {"name": "example-host", "addresses": ["100.64.0.5"]})


def loopback(
    *runtimes: tuple[str, dict], grants: tuple[dict, ...] = (GRANT,), tests: tuple[dict, ...] = (TEST,), **example
) -> Example:
    """The machine at 100.64.0.5, grants to its ports and the policy's tests, as
    the document writes them, and its containers as Portainer read them."""

    return Example(
        readings={
            **policy(
                hosts={"example-host": "100.64.0.5"},
                grants=list(grants),
                tests=list(tests),
                document=json.dumps(
                    {"grants": [{**grant, "srcPosture": ["posture:latest"]} for grant in grants], "tests": list(tests)}
                ),
                devices=({"name": "example-host", "addresses": ["100.64.0.5"]},),
            ),
            "portainer.container": tuple(
                {"connection_ref": "example-portainer", "host": "example-host", "name": name, "state": "running"}
                for name, _fields in runtimes
            ),
            "portainer.runtime": tuple(
                {
                    "connection_ref": "example-portainer",
                    "host": "example-host",
                    "container": name,
                    "network_mode": "bridge",
                    **fields,
                }
                for name, fields in runtimes
            ),
        },
        given=example.pop("given", a_machine),
        **example,
    )


def trusting(*trusted: str, addresses: tuple[str, ...], routes: tuple[str, ...] = (), raises=()) -> Example:
    return Example(
        readings={DEVICES: ({"name": "example-host", "addresses": list(addresses), "enabled_routes": list(routes)},)},
        settings={"SEVERINO_TRUSTED_NETWORKS": list(trusted)},
        raises=raises,
    )


def bound(host_ip: str, port: int = 5432) -> dict:
    return {"port_bindings": [{"container_port": f"{port}/tcp", "host_port": str(port), "host_ip": host_ip}]}


def read_on_loopback() -> None:
    """The machine, and a connection HQ reads its database through over loopback."""

    a_machine()
    connection("adguard", "example-dns", controller_id="example-host", endpoint="http://127.0.0.1:5432")


GRANT_5432 = {**GRANT, "ip": ["tcp:5432"]}
# Two grants, and the one test that accepts what both open.
FROM_MAC = {**GRANT, "ip": ["tcp:443", "tcp:5432"]}
FROM_PHONE = {"src": ["example-phone"], "dst": ["example-host"], "ip": ["tcp:6379"]}
ACCEPTS_BOTH = {
    "src": "example-mac",
    "proto": "tcp",
    "accept": ["example-host:443", "example-host:5432", "example-host:6379"],
}

EXAMPLES = (
    Examples(
        "tag-granted-to-nobody",
        fires=(
            # One unworn tag is named.
            Example(
                readings=granted("tag:worn", "tag:unworn"),
                raises=("tag:unworn is granted access but no device has it",),
            ),
            # Several are counted, and each is named in the step.
            Example(
                readings=granted("tag:one", "tag:two"), raises=("2 tags are granted access but no device has them",)
            ),
        ),
        quiet=(Example(readings=granted("tag:worn")),),
    ),
    Examples(
        "tailnet-dns-off-tailnet",
        fires=(
            # A nameserver that is no device address is off the tailnet.
            Example(
                readings={
                    DEVICES: ({"name": "example-host", "addresses": ["100.64.0.53", "fd7a:115c:a1e0::35"]},),
                    DNS: ({"record": "dns", "nameservers": ["100.64.0.53", "192.0.2.53"]},),
                },
                raises=("Tailnet DNS points at 192.0.2.53, which is not a tailnet address",),
            ),
        ),
        quiet=(
            Example(
                readings={
                    DEVICES: ({"name": "example-host", "addresses": ["100.64.0.53"]},),
                    DNS: ({"record": "dns", "nameservers": ["100.64.0.53"]},),
                }
            ),
            # Without a device reading no resolver is judged.
            Example(readings={DNS: ({"record": "dns", "nameservers": ["192.0.2.53"]},)}),
        ),
    ),
    Examples(
        "devices-join-without-approval",
        fires=(
            Example(
                readings=policy(settings={"devicesApprovalOn": False}, grants=[{"src": ["*"], "dst": ["*"]}]),
                raises=("New devices join the tailnet without approval",),
            ),
            # With lock off, approval off is what lets a device in.
            Example(
                readings=policy(settings={"devicesApprovalOn": False}, lock={"enabled": False}),
                raises=("New devices join the tailnet without approval",),
            ),
        ),
        quiet=(
            Example(readings=policy(settings={"devicesApprovalOn": True})),
            # Unread.
            Example(readings=policy(settings={})),
            # Tailnet lock is approval by signature, and Tailscale turns approval off beside it.
            Example(
                readings=policy(
                    settings={"devicesApprovalOn": False},
                    lock={"enabled": True, "signers": [{"key": "tlpub:01"}, {"key": "tlpub:02"}]},
                )
            ),
        ),
    ),
    Examples(
        "empty-group-granted",
        fires=(
            # An empty group a grant names is a cleanup hint.
            Example(
                readings=policy(
                    groups=[
                        {"name": "group:empty", "members": []},
                        {"name": "group:admins", "members": ["someone@example.com"]},
                        {"name": "group:unused", "members": []},
                    ],
                    grants=[{"src": ["group:empty", "group:admins"], "dst": ["tag:server"], "ip": ["tcp:22"]}],
                ),
                raises=("group:empty has no members but is still granted access",),
            ),
            # A shell rule counts as a grant.
            Example(
                readings=policy(
                    groups=[{"name": "group:ops", "members": []}],
                    ssh_rules=[{"action": "check", "src": ["group:ops"], "dst": ["tag:server"], "users": ["root"]}],
                ),
                raises=("group:ops has no members but is still granted access",),
            ),
            # Several are counted, and each is named in the step.
            Example(
                readings=policy(
                    groups=[{"name": "group:one", "members": []}, {"name": "group:two", "members": []}],
                    grants=[{"src": ["group:one", "group:two"], "dst": ["tag:server"], "ip": ["tcp:22"]}],
                ),
                raises=("2 groups have no members but are still granted access",),
            ),
        ),
        quiet=(
            Example(
                readings=policy(
                    groups=[{"name": "group:admins", "members": ["someone@example.com"]}],
                    grants=[{"src": ["group:admins"], "dst": ["tag:server"], "ip": ["tcp:22"]}],
                )
            ),
        ),
    ),
    Examples(
        "grant-reaches-only-loopback",
        fires=(
            # HQ's own port on its machine, with the grant and its test as written, without the port.
            loopback(
                PROXY,
                hq_at=("100.64.0.5",),
                raises=("A grant opens tcp:8000 on example-host, where Severino HQ is bound only to 127.0.0.1",),
            ),
            # A port every container binds to loopback.
            loopback(
                PROXY,
                ("example-db", bound("127.0.0.1")),
                grants=({**GRANT, "ip": ["tcp:443", "tcp:5432"]},),
                raises=("A grant opens tcp:5432 on example-host, where example-db is bound only to 127.0.0.1",),
            ),
            # Two grants crossing one test, a connection HQ reads over loopback
            # naming a port without serving it beyond: each grant edited, and
            # the test replaced once with every port it accepted denied.
            loopback(
                PROXY,
                ("example-db", bound("127.0.0.1")),
                ("example-cache", bound("127.0.0.1", 6379)),
                grants=(FROM_MAC, FROM_PHONE),
                tests=(ACCEPTS_BOTH,),
                given=read_on_loopback,
                raises=(
                    Raised(
                        title="2 granted ports are bound only to loopback",
                        steps=(
                            Do(
                                "In the Tailscale admin console, open Access controls and replace the grant from "
                                "example-mac to example-host with this one.",
                                value=json.dumps(
                                    {
                                        "src": ["example-mac"],
                                        "dst": ["example-host"],
                                        "ip": ["tcp:443"],
                                        "srcPosture": ["posture:latest"],
                                    }
                                ),
                            ),
                            Do(
                                "In the Tailscale admin console, open Access controls and remove the grant from "
                                "example-phone to example-host."
                            ),
                            Do(
                                "Replace the policy test for example-mac with this one, which denies "
                                "example-host:5432 and example-host:6379.",
                                value=json.dumps(
                                    {
                                        "src": "example-mac",
                                        "proto": "tcp",
                                        "accept": ["example-host:443"],
                                        "deny": ["example-host:5432", "example-host:6379"],
                                    }
                                ),
                                notes=(
                                    "Tailscale rejects a policy whose test fails, so the port cannot be opened again by mistake.",
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        ),
        quiet=(
            # A port something serves on another address.
            loopback(("example-db", bound("127.0.0.1")), ("example-other", bound("0.0.0.0")), grants=(GRANT_5432,)),
            loopback(("example-db", bound("127.0.0.1")), ("example-other", bound("100.64.0.5")), grants=(GRANT_5432,)),
            loopback(
                ("example-db", bound("127.0.0.1")),
                ("example-other", {"network_mode": "host", "exposed_ports": [5432]}),
                grants=(GRANT_5432,),
            ),
            # HQ's port where another host-network container exposes it.
            loopback(("example-app", {"network_mode": "host", "exposed_ports": [8000]}), hq_at=("100.64.0.5",)),
            # A machine whose containers were not read.
            loopback(hq_at=("100.64.0.5",)),
        ),
    ),
    Examples(
        "trusted-wider-than-tailnet",
        fires=(
            # Trusting the whole range, with what the tailnet uses.
            trusting(
                "127.0.0.0/8",
                TAILNET_V4,
                addresses=("100.64.0.1", "100.64.0.2"),
                routes=("192.0.2.0/24",),
                raises=("HQ trusts all of 100.64.0.0/10, and the tailnet uses 2 device addresses and 1 subnet route",),
            ),
            # Only a wide range a device address falls in is narrowed.
            trusting(
                "::1/128",
                TAILNET_V6,
                addresses=("100.64.0.1", "fd7a:115c:a1e0::1"),
                raises=("HQ trusts all of fd7a:115c:a1e0::/48, and the tailnet uses 1 device address",),
            ),
            trusting(
                TAILNET_V4,
                TAILNET_V6,
                addresses=("100.64.0.1",),
                raises=("HQ trusts all of 100.64.0.0/10, fd7a:115c:a1e0::/48, and the tailnet uses 1 device address",),
            ),
            # An invalid trusted entry is skipped.
            trusting(
                "not a network",
                TAILNET_V4,
                addresses=("100.64.0.1",),
                raises=("HQ trusts all of 100.64.0.0/10, and the tailnet uses 1 device address",),
            ),
        ),
        quiet=(
            # Trust already narrowed.
            trusting("127.0.0.0/8", "100.64.0.1/32", addresses=("100.64.0.1",)),
            trusting("::1/128", "fd7a:115c:a1e0::1/128", addresses=("fd7a:115c:a1e0::1",)),
            # Without a device reading.
            Example(settings={"SEVERINO_TRUSTED_NETWORKS": [TAILNET_V4]}),
        ),
    ),
)

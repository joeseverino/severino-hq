"""The request standard: each independent control a request passed, read from what enforces it.

Its subject is ``RequestFacts``, what HQ read about one request. Its checks are
declared once in ``REQUEST_STANDARD``, outermost first, each with the hop of
the path it is decided at, and measured as every standard is (``standards``).
"""

from dataclasses import KW_ONLY, dataclass, field
from datetime import datetime
from typing import Any

from django.conf import settings
from django.utils.csp import CSP
from django.utils.text import capfirst

from hq.domains.control_plane.names import is_hostname
from hq.platform.application.labels import dotted
from hq.platform.core.network import split_host_port
lazy from hq.domains.control_plane.connection_kinds import CONNECTION_LABELS
lazy from hq.domains.control_plane.providers import PROVIDERS

from . import tailnet
from .connection_security import EDGE_LABEL, FIREWALL_LABEL
from .doors import approved_words
from .freshness import PAGE_VISIT, freshness
from .moments import expiry_words, moment
from .posture import SIGN_IN, STANDARD, advanced
from .request_channel import UNTRUSTED_FORWARDER, Channel, encryption
from .request_identity import SSO_LINK, Identity
from .standards import Check, Measurement, Reading, Standing, measure
from .tone import Tone
from .ui import counted, ended
lazy from .zones import public_answers_for

# A request whose Host is an address names nothing public DNS could answer for.
ADDRESS_HOST = "This request used an address."


def owned_by(device: tailnet.Device) -> str:
    """Whose a tailnet device is, as the tailnet reports it."""

    return f"owned by {device.user}" if device.user else "no reported owner"


@dataclass(frozen=True, slots=True)
class ServingDeviceResolution:
    """The node serving HQ and how strongly that placement was established."""

    device: tailnet.Device | None
    _: KW_ONLY
    verified: bool
    basis: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RequestFacts:
    """What HQ read about one request, once: the subject its standard measures
    and the connection page describes."""

    request: Any
    address: str
    channel: Channel
    identity: Identity
    host: str = ""
    secure: bool = False
    # The socket peer, which is the forwarding proxy when one forwarded it.
    peer: str = ""
    forwarded: bool = False
    forwarding_trusted: bool = False
    # The tailnet as swept, read once for every question asked of it.
    known: dict[str, tailnet.Device] = field(default_factory=dict)
    device: tailnet.Device | None = None
    forwarder: tailnet.Device | None = None
    # A device correlated from a forwarded address that HQ shows as evidence
    # but never uses for admission until the forwarding peer is a declared
    # proxy, so display knowledge never quietly becomes authorization.
    reported_device: tailnet.Device | None = None
    reported_address: str = ""
    serving: ServingDeviceResolution = ServingDeviceResolution(None, verified=False, basis="not found")
    observer: tailnet.Device | None = None
    # The caller device's tailnet presence, as the machine page reads it.
    presence: Any = None
    # The last tailnet lock reading; empty when nothing read it.
    lock: dict[str, Any] = field(default_factory=dict)
    # What HQ was told about its machines.
    declared: tuple = ()
    # The proxy's access-list control for HQ's name, and the host firewall's
    # for its port; None where nothing observed them.
    edge: Any = None
    firewall: Any = None

    @property
    def untrusted_forwarding(self) -> bool:
        return self.forwarded and not self.forwarding_trusted

    @property
    def serves(self) -> tailnet.Device | None:
        """HQ's node, only where it was placed: a fallback observer is
        provenance, never HQ's policy target or endpoint."""

        return self.serving.device if self.serving.verified else None

    @property
    def tailnet_observed_at(self) -> datetime | None:
        """When the device/path evidence was last swept from Tailscale."""

        if self.presence is not None:
            return self.presence.observed_at
        return self.observer.observed_at if self.observer is not None else None

    @property
    def tailnet_current(self) -> bool:
        """Whether the tailnet reading is recent enough that opening the panel
        would not ask for it again. Only then is a value read from it said as
        it is now; otherwise as it was when last read, which is no fault."""

        observed = self.tailnet_observed_at
        return observed is not None and not freshness(PAGE_VISIT, observed).due


def measure_request(facts: RequestFacts) -> Measurement:
    """``facts`` against the request standard. A check of HQ's standard among
    them is an open item, shown quietly, unless Advanced HQ Posture is on."""

    return measure(facts, REQUEST_STANDARD, quiet=() if advanced() else STANDARD)


# ----- The checks -----------------------------------------------------------------


def _control(control) -> Reading:
    """A shared provider control, as the request reads it: "neutral" is the
    reading that never came, which is neither a pass nor a denial."""

    if control.state == Tone.GOOD:
        return Reading.met(control.evidence, detail=control.detail)
    if control.state == Tone.NEUTRAL:
        return Reading.unavailable(control.evidence, detail=control.detail)
    return Reading.unmet(control.evidence, detail=control.detail)


def _name(facts: RequestFacts) -> Reading:
    """Whether the name in the address bar is one the internet can resolve."""

    host, _port = split_host_port(facts.host)
    if not is_hostname(host):
        return Reading.unavailable(host, detail=ADDRESS_HOST)
    answers = public_answers_for(host)
    if answers:
        return Reading.unmet(
            f"{host} resolves to {', '.join(answers)}", detail="A public DNS record exists for this name."
        )
    return Reading.met(host, detail="No public DNS record exists for this name in any domain HQ reads.")


def _edge(facts: RequestFacts) -> Reading:
    return _control(facts.edge)


def _arrival(facts: RequestFacts) -> Reading:
    """Whether the address was required, or only presented: the host
    firewall's own rule for HQ's port, accepted only from packets that arrived
    on the tailnet interface, which no sender can set."""

    return _control(facts.firewall)


def _channel(facts: RequestFacts) -> Reading:
    return Reading.judged(facts.channel.id == "tailnet", evidence=facts.address, detail=facts.channel.detail)


def _proxied(facts: RequestFacts) -> bool:
    """A forwarded request through a tailnet proxy, whose two tailnet hops
    the policy governs apart."""

    return facts.forwarded and facts.forwarder is not None


def _policy(facts: RequestFacts) -> Reading:
    """Being on the tailnet is not being allowed to reach everything on it.
    Answered by Tailscale during the sweep rather than worked out here: a
    second implementation of an access policy is believed exactly as much as
    the real one and wrong where nobody looks.

    A reverse proxy on HQ's own host forwards from loopback, which the tailnet
    never sees, so the one hop it governs is the caller's device to HQ's node
    on the port they dialled."""

    port = _scheme_port(facts) if facts.forwarded else _port_of(facts)
    return _may_reach(facts.device, facts.serves, port, facts.known)


def _edge_policy(facts: RequestFacts) -> Reading:
    return _may_reach(facts.device, facts.forwarder, _scheme_port(facts), facts.known)


def _service_policy(facts: RequestFacts) -> Reading:
    return _may_reach(facts.forwarder, facts.serves, _server_port(facts), facts.known)


def _may_reach(
    source: tailnet.Device | None, target: tailnet.Device | None, port: int, known: dict[str, tailnet.Device]
) -> Reading:
    if source is None or target is None:
        missing = "your device" if source is None else "HQ's machine"
        return Reading.unavailable(
            dotted(f"port {port}", f"{missing} not found"), detail=f"{capfirst(missing)} is not on the tailnet HQ read."
        )
    # Names for the lookup, labels for the sentence: the policy is keyed on the
    # node's registered name, but a person reads the MagicDNS label, and two
    # phones registered as "localhost" are indistinguishable in the other one.
    verdict = tailnet.may_reach(source.name, target.name, port, known)
    owners = tailnet.alias_owners(known)
    if not verdict.known:
        return Reading.unavailable(f"{source.label} to {target.label} on {port}", detail=verdict.detail)
    # The grant itself: the rule is what an operator would go and change.
    rules = tuple(
        f"{', '.join(tailnet.as_devices(rule.get('who') or ['?'], owners))} to "
        f"{'; '.join(tailnet.by_device(rule.get('to') or ['?'], owners))}"
        for rule in verdict.rules
    )
    evidence = ", ".join(tailnet.as_devices(verdict.via, owners)) or f"port {port}"
    return Reading.judged(verdict.allowed, evidence=evidence, detail=verdict.detail, rules=rules)


def _scheme_port(facts: RequestFacts) -> int:
    return 443 if facts.secure else 80


def _port_of(facts: RequestFacts) -> int:
    _host, port = split_host_port(facts.host)
    return int(port) if port.isdigit() else _scheme_port(facts)


def _server_port(facts: RequestFacts) -> int:
    """The port the forwarding peer actually reached on HQ."""

    value = str(facts.request.META.get("SERVER_PORT", "") or "")
    return int(value) if value.isdigit() else _port_of(facts)


def _device(facts: RequestFacts) -> Reading:
    """Being in the inventory means the tailnet has seen this machine, not
    that it will carry traffic for it: a device pending approval is admitted
    to nothing, and under tailnet lock an unsigned node is filtered out by
    every peer while reporting itself healthy."""

    device = facts.device
    if device is None and not facts.known:
        return Reading.unavailable("", detail="HQ has not read the tailnet.")
    if device is None and not facts.tailnet_current:
        # A device that joined or changed address since an old reading is
        # missing from it, which is no disagreement.
        return Reading.unavailable("", detail="The last tailnet read has no device at this address.")
    if device is None:
        return Reading.unmet(detail="No device on the tailnet has this address.")
    named = device.dns_name or device.name
    if not device.authorized:
        return Reading.unmet(named, detail=f"{device.label} is on the tailnet but has not been approved.")
    if device.lock_error:
        return Reading.unmet(
            named,
            detail=f"{device.label} is not signed for tailnet lock, so other devices ignore it. {device.lock_error}",
        )
    expiry = (
        ended(f" Its node key {_expiry_phrase(device.key_expires)}")
        if device.key_expires
        else " Its node key does not expire."
    )
    return Reading.met(named, detail=f"{device.label} is on the tailnet, {owned_by(device)}, and approved.{expiry}")


def _locked(facts: RequestFacts) -> bool:
    """A device with a lock reading: with no device, the check above already
    said no node answers; with no reading, lock is not known to be off."""

    return facts.device is not None and bool(facts.lock)


def _lock(facts: RequestFacts) -> Reading:
    """Whether this node's key was signed, or merely handed out: every check
    above trusts that the coordination server distributed the right key, and
    tailnet lock alone removes that trust."""

    device = facts.device
    lock = facts.lock
    if not lock.get("enabled"):
        return Reading.unavailable("Lock off", detail="Tailnet lock is off, so node keys are not signed.")
    if device.lock_error:
        return Reading.unmet(
            "Unsigned",
            detail=f"{device.label} has no valid tailnet lock signature, so other devices ignore it. {device.lock_error}",
        )
    return Reading.met(
        dotted("Signed", counted(len(lock.get("signers") or ()), "signing key", "signing keys")),
        detail=f"Tailnet lock is on and {device.label}'s node key has a valid signature.",
    )


# What an agent's token call is told of its device, by its standing
# (``agent_devices.DeviceStanding``): a token has no session to compare, so the
# person's approval of the device for that agent is the link between them and
# its tailnet owner.
_AGENT_DEVICE = {
    "approved": (Standing.MET, "{approver} approved {agent} on this device."),
    "awaiting": (Standing.UNMET, "{agent} is not approved on this device yet. Approve it on HQ's Agents page."),
    "blocked": (Standing.UNMET, "{agent} is blocked on this device. Allow it again on HQ's Agents page."),
    "unplaceable": (Standing.UNMET, "The tailnet could not place {agent}'s call on a device, so it was refused."),
    "unplaced": (Standing.UNAVAILABLE, "This deployment has no tailnet, so {agent}'s own rules apply."),
}


def _is_agent(facts: RequestFacts) -> bool:
    return bool(facts.identity.agent)


def _is_person(facts: RequestFacts) -> bool:
    return not facts.identity.agent


def _agent_device(facts: RequestFacts) -> Reading:
    """Whether a person approved the device an agent's call came from for it."""

    identity = facts.identity
    standing, detail = _AGENT_DEVICE[identity.device_standing]
    evidence = dotted(
        identity.device_standing.capitalize(),
        f"device owner {identity.tailnet_user}" if identity.tailnet_user else "",
        approved_words(identity.approved_by),
    )
    said = detail.format(agent=identity.agent, approver=identity.approved_by or "An operator")
    if standing is Standing.UNAVAILABLE:
        return Reading.unavailable(evidence, detail=said)
    return Reading.judged(standing is Standing.MET, evidence=evidence, detail=said)


def _owner(facts: RequestFacts) -> Reading:
    """The independently observed device owner beside the signed-in person."""

    identity = facts.identity
    if not identity.tailnet_user:
        return Reading.unavailable("No device owner reported", detail="The tailnet reports no owner for this device.")
    session_principal = identity.email or identity.username or "unnamed session"
    paired = f"{identity.tailnet_user} ↔ {session_principal}"
    if not identity.corroborated and not identity.conflicted:
        return Reading.unavailable(
            paired, detail="Single sign-on did not link this device's tailnet owner to your HQ account."
        )
    if identity.agreement_basis == SSO_LINK:
        return Reading.met(
            dotted(
                f"Device owner {identity.tailnet_user}",
                f"single sign-on {identity.provider_principal}",
                f"signed in as {session_principal}",
            ),
            detail="You signed in with your Tailscale account, and Tailscale says that account owns this device.",
        )
    if identity.corroborated:
        return Reading.met(paired, detail="The tailnet account that owns this device is the account signed in to HQ.")
    return Reading.unmet(
        paired,
        detail="A different tailnet account owns this device. Check who owns the device and who is signed in.",
    )


def _forwarding(facts: RequestFacts) -> bool:
    return facts.forwarded


def _forwarder(facts: RequestFacts) -> Reading:
    device = facts.forwarder
    if not facts.forwarding_trusted:
        return Reading.unmet(facts.peer, detail=UNTRUSTED_FORWARDER)
    if device is None:
        return Reading.met(facts.peer, detail="A trusted proxy.")
    return Reading.met(
        device.dns_name or device.name,
        detail=f"{device.label} holds this address, which is on HQ's list of trusted proxies.",
    )


def _forwarding_spec():
    """The proxy kind that declares the headers it forwards, if any does."""

    return next((spec for spec in PROVIDERS.values() if spec.forwarding_headers), None)


def _proxy_headers_apply(facts: RequestFacts) -> bool:
    return facts.forwarded and facts.forwarding_trusted and _forwarding_spec() is not None


def _proxy_headers(facts: RequestFacts) -> Reading:
    """A proxy's redundant forwarding headers, cross-checked without granting
    them authority: the ones its kind declares as ``forwarding_headers``."""

    declared = _forwarding_spec()
    client_header, scheme_header = declared.forwarding_headers
    proxy = next(
        (CONNECTION_LABELS[name] for name in declared.connection_providers if name in CONNECTION_LABELS),
        declared.label,
    )

    def header(name: str) -> str:
        return str(facts.request.META.get(f"HTTP_{name.upper().replace('-', '_')}", "") or "").strip()

    real = header(client_header)
    scheme = header(scheme_header).lower()
    if not real or not scheme:
        return Reading.unavailable(
            "Headers incomplete", detail=ended(f"The proxy did not send both {client_header} and {scheme_header}")
        )
    real_host = split_host_port(real)[0]
    expected_scheme = "https" if facts.secure else "http"
    agrees = real_host == facts.address and scheme == expected_scheme
    return Reading.judged(
        agrees,
        evidence=dotted(f"{client_header}={real_host or 'missing'}", f"{scheme_header}={scheme or 'missing'}"),
        detail=(
            f"{proxy}'s {client_header} and {scheme_header} headers match the address and scheme HQ used."
            if agrees
            else f"{proxy}'s {client_header} or {scheme_header} header differs "
            "from the address or scheme HQ used. Check the proxy's forwarding settings."
        ),
    )


def _observer(facts: RequestFacts) -> Reading:
    """Whether observer-relative link data is evidence about HQ."""

    observer, serving = facts.observer, facts.serving
    if observer is None:
        return Reading.unavailable("", detail="The tailnet reading does not say which machine measured this link.")
    if not serving.verified:
        return Reading.unavailable(
            dotted(f"Measured by {observer.label}", "HQ's machine not found"),
            detail="HQ could not find its own tailnet machine.",
        )
    if serving.device and observer.name == serving.device.name:
        return Reading.met(
            dotted(observer.label, serving.basis),
            detail="The machine that measured this link is the machine HQ runs on.",
        )
    return Reading.unmet(
        dotted(f"Measured by {observer.label}", f"HQ runs on {serving.device.label}"),
        detail="A different machine measured this link.",
    )


def _expiry_phrase(stamp: str) -> str:
    """When a node key runs out, in the tense that fits."""

    parsed = moment(stamp)
    return "has an expiry HQ could not read" if parsed is None else expiry_words(parsed)


def _gate(facts: RequestFacts) -> Reading:
    """Whether HQ refuses everything else before it authenticates anything."""

    enforced = settings.SEVERINO_ENFORCE_TRUSTED_NETWORK
    if enforced and facts.channel.id == "opaque":
        return Reading.unmet(
            "Checking a proxy",
            detail="The proxy did not pass your address on. Anyone who can reach the proxy passes.",
        )
    return Reading.judged(
        enforced and facts.channel.private,
        evidence="Enforced" if enforced else "Not enforced",
        detail=(
            "HQ refuses any address outside its private ranges before sign-in."
            if enforced
            else "HQ is not checking which address a request comes from."
        ),
    )


def _session(_facts: RequestFacts) -> Reading:
    """Whether the cookie carrying this session can leave where it was set."""

    secure = bool(settings.SESSION_COOKIE_SECURE)
    http_only = bool(settings.SESSION_COOKIE_HTTPONLY)
    same_site = str(settings.SESSION_COOKIE_SAMESITE or "")
    name = settings.SESSION_COOKIE_NAME
    # The `__Host-` prefix is the only one of these the browser enforces
    # against somebody else: a promise no sibling host can have written it.
    holds = secure and http_only and bool(same_site) and name.startswith("__Host-")
    return Reading.judged(
        holds,
        evidence=dotted(
            f"Secure={'on' if secure else 'off'}",
            f"HttpOnly={'on' if http_only else 'off'}",
            f"SameSite={same_site or 'unset'}",
            name or "unnamed",
        ),
        detail=(
            "HTTPS only, hidden from scripts, never sent from another site, and set only by this host."
            if holds
            else "The session cookie is missing a protection. Check Secure, HttpOnly, SameSite and the __Host- prefix."
        ),
    )


def _browser(_facts: RequestFacts) -> Reading:
    """What HQ tells the browser it is allowed to do with this page: the one
    boundary HQ cannot check from the inside, since it is enforced in someone
    else's browser, so the policy is stated here and violations reported back."""

    policy = dict(settings.SECURE_CSP or {})
    trusted_types = "'script'" in (policy.get("require-trusted-types-for") or ())
    scripts = tuple(policy.get("script-src") or ())
    # The nonce alone decides: 'strict-dynamic' makes a CSP3 browser ignore
    # 'self' and host sources, and nothing may allow inline script.
    nonce_only = CSP.NONCE in scripts and CSP.STRICT_DYNAMIC in scripts and CSP.UNSAFE_INLINE not in scripts
    framed = tuple(policy.get("frame-ancestors") or ()) == (CSP.NONE,)
    reports = bool(policy.get("report-uri") or policy.get("report-to"))
    holds = trusted_types and nonce_only and framed
    return Reading.judged(
        holds,
        evidence=dotted(
            "Trusted Types" if trusted_types else "no Trusted Types",
            "nonce and strict-dynamic" if nonce_only else "scripts by origin",
            "no framing" if framed else "framing allowed",
            "reported" if reports else "unreported",
        ),
        detail=(
            "Only scripts HQ tagged with a one-time code run. The page cannot be framed, and Trusted Types is on."
            if holds
            else "The content security policy is missing a rule that stops scripts HQ did not send."
        ),
    )


def _canonical(_facts: RequestFacts) -> Reading:
    """Whether plain HTTP is a way in, and whether the browser is told it
    isn't: HQ binds a plain port behind a proxy that terminates TLS, so only
    refusing plain HTTP and sending HSTS make "HTTPS" a fact about HQ."""

    redirected = bool(settings.SECURE_SSL_REDIRECT)
    hsts = int(settings.SECURE_HSTS_SECONDS or 0)
    subdomains = bool(settings.SECURE_HSTS_INCLUDE_SUBDOMAINS)
    holds = redirected and hsts > 0
    return Reading.judged(
        holds,
        evidence=dotted(
            "plain HTTP redirected" if redirected else "plain HTTP served",
            f"HSTS {hsts // 86400} days" if hsts else "HSTS not sent",
            "subdomains included" if hsts and subdomains else "",
        ),
        detail=(
            "HQ redirects plain HTTP to HTTPS and tells the browser to use HTTPS only for this name."
            if holds
            else "HQ serves plain HTTP on its port."
            if not redirected
            else "HQ redirects plain HTTP and sends no HSTS header."
        ),
    )


def _transport(facts: RequestFacts) -> Reading:
    tls = facts.secure
    over_tailnet = facts.channel.id == "tailnet"
    if over_tailnet and tls:
        detail = "WireGuard encrypts the link, and TLS encrypts the request inside it."
    elif over_tailnet:
        detail = "WireGuard encrypts this request. There is no TLS inside it."
    elif tls:
        detail = "TLS encrypts this request. The caller is not on the tailnet."
    else:
        detail = "This request reached HQ without TLS."
    return Reading.judged(tls or over_tailnet, evidence=encryption(facts.channel, tls), detail=detail)


# Read again by the proxy's hop, whose check the headers settle.
PROXY_HEADERS = Check(
    "proxy-evidence",
    "The proxy's headers agree",
    _proxy_headers,
    "",
    "",
    applies=_proxy_headers_apply,
    unmet="The proxy's headers disagree",
    hop="ingress",
    boundary="Forwarding evidence",
    mechanism="The proxy's forwarding headers",
)

# Each check as a request meets it rather than by importance, so reading down
# is reading the path in: no single one is the reason HQ is not on the
# internet. A check only the connection panel draws says no ``why`` or ``fix``.
REQUEST_STANDARD: tuple[Check, ...] = (
    Check(
        "name",
        "The name is not in public DNS",
        _name,
        "",
        "",
        unmet="The name is in public DNS",
        hop="dns",
        boundary="Exposure",
        mechanism="Public DNS records",
    ),
    Check(
        "edge",
        EDGE_LABEL,
        _edge,
        "",
        "",
        applies=lambda facts: facts.edge is not None,
        unmet="The proxy lets in more than the tailnet",
        hop="ingress",
        boundary="Reverse proxy edge",
        mechanism="The proxy's access list",
    ),
    Check(
        "channel",
        "The address is on the tailnet",
        _channel,
        "",
        "",
        unmet="The address is not on the tailnet",
        hop="network",
        boundary="Network",
        mechanism="The tailnet address range",
    ),
    # Against the address claim above, because that is what it qualifies:
    # whether the packet had to arrive on the tailnet to be let in. Absent
    # where nothing observed the firewall, rather than a verdict from silence.
    Check(
        "arrival",
        FIREWALL_LABEL,
        _arrival,
        "",
        "",
        applies=lambda facts: facts.firewall is not None,
        unmet="The firewall accepts HQ's port by address alone",
        hop="machine",
        boundary="Network",
        mechanism="The firewall rule",
    ),
    Check(
        "policy",
        "The tailnet policy allows this device",
        _policy,
        "",
        "",
        applies=lambda facts: not _proxied(facts),
        unmet="The tailnet policy does not allow this device",
        hop="network",
        boundary="Zero trust policy",
        mechanism="The tailnet policy",
    ),
    Check(
        "edge-policy",
        "The tailnet policy allows you to reach the proxy",
        _edge_policy,
        "",
        "",
        applies=_proxied,
        unmet="The tailnet policy does not allow you to reach the proxy",
        hop="network",
        boundary="Zero trust policy",
        mechanism="The tailnet policy",
    ),
    Check(
        "service-policy",
        "The tailnet policy allows the proxy to reach HQ",
        _service_policy,
        "",
        "",
        applies=_proxied,
        unmet="The tailnet policy does not allow the proxy to reach HQ",
        hop="ingress",
        boundary="Zero trust policy",
        mechanism="The tailnet policy",
    ),
    Check(
        "device",
        "The tailnet knows this device",
        _device,
        "",
        "",
        unmet="The tailnet does not admit this device",
        hop="device",
        boundary="Device identity",
        mechanism="The tailnet device list",
    ),
    # Under the device claim, because it is what that claim rests on.
    Check(
        "tailnet-lock",
        "Node key signed by tailnet lock",
        _lock,
        "",
        "",
        applies=_locked,
        unmet="The node key is not signed by tailnet lock",
        hop="device",
        boundary="Device identity",
        mechanism="Tailnet lock signature",
    ),
    Check(
        "identity-agreement",
        "You own this device",
        _owner,
        "",
        "",
        applies=_is_person,
        unmet="A different tailnet account owns this device",
        hop="device",
        boundary="Identity correlation",
        mechanism="The session and the tailnet device owner",
    ),
    Check(
        "identity-agreement",
        "This device is approved for the agent",
        _agent_device,
        "",
        "",
        applies=_is_agent,
        unmet="This device is not approved for the agent",
        hop="device",
        boundary="Identity correlation",
        mechanism="Device approval on the Agents page",
    ),
    Check(
        "forwarder",
        "HQ trusts the proxy that forwarded this",
        _forwarder,
        "",
        "",
        applies=_forwarding,
        unmet="An untrusted proxy forwarded this",
        hop="ingress",
        boundary="Forwarding identity",
        mechanism="HQ's list of trusted proxies",
    ),
    PROXY_HEADERS,
    Check(
        "tailnet-observer",
        "HQ's own machine measured this link",
        _observer,
        "",
        "",
        unmet="Another machine measured this link",
        hop="network",
        boundary="Transport attestation",
        mechanism="Tailscale on the machine that read the tailnet",
    ),
    Check(
        "gate",
        "Other networks are refused",
        _gate,
        "",
        "",
        unmet="Other networks are not refused",
        hop="hq",
        boundary="Application edge",
        mechanism="HQ's trusted-network setting",
    ),
    SIGN_IN,
    Check(
        "session",
        "Session cookie is locked to this site",
        _session,
        "",
        "",
        unmet="The session cookie is missing a protection",
        hop="hq",
        boundary="Session",
        mechanism="HQ's cookie settings",
    ),
    Check(
        "transport",
        "The connection is encrypted",
        _transport,
        "",
        "",
        unmet="The connection is not encrypted",
        hop="network",
        boundary="Transport",
        mechanism="WireGuard and TLS",
    ),
    Check(
        "canonical",
        "HTTPS only",
        _canonical,
        "",
        "",
        unmet="Plain HTTP is a way in",
        hop="hq",
        boundary="Transport",
        mechanism="HTTPS redirect and HSTS",
    ),
    Check(
        "browser",
        "Scripts only from HQ",
        _browser,
        "",
        "",
        unmet="The content security policy is missing a rule",
        hop="hq",
        boundary="Browser boundary",
        mechanism="Content Security Policy",
    ),
)

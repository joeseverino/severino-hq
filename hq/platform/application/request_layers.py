"""The admission layers: each independent control a request passed, read from what enforces it."""

from dataclasses import dataclass

from django.conf import settings
from django.utils.csp import CSP

from hq.platform.core.network import split_host_port

from . import tailnet
from .request_channel import Channel
from .request_identity import Identity
from .timestamps import moment
from .ui import counted


@dataclass(frozen=True)
class Layer:
    """One thing that had to hold, and what HQ can point at to say it did.

    ``holds`` is what the badge and the ordering read. ``evidence`` is the
    value it was decided from, kept separate from ``detail`` so the reasoning
    and the reading are never confused for each other: the whole failure this
    page exists to avoid is prose that sounds like a measurement.
    """

    id: str
    label: str
    holds: bool
    detail: str
    evidence: str = ""
    # The security boundary and concrete mechanism this verdict belongs to.
    # These are emitted with the verdict so every UI can explain the same
    # decision without maintaining a second taxonomy beside it.
    boundary: str = ""
    mechanism: str = ""
    # False means the evidence source did not answer this question. That is
    # neither a pass nor a denial and must never be rendered as either one.
    conclusive: bool = True
    # The rules behind the verdict, where the thing deciding it had rules.
    rules: tuple[str, ...] = ()

    @property
    def state(self) -> str:
        if not self.conclusive:
            return "unknown"
        return "holds" if self.holds else "does-not"


@dataclass(frozen=True)
class ServingDeviceResolution:
    """The node serving HQ and how strongly that placement was established."""

    device: tailnet.Device | None
    verified: bool
    basis: str


def admission_layers(
    request,
    address: str,
    channel: Channel,
    device: tailnet.Device | None,
    forwarder: tailnet.Device | None,
    serves: tailnet.Device | None,
    identity: Identity,
    known: dict[str, tailnet.Device],
    *,
    forwarded: bool,
    forwarding_trusted: bool,
    forwarding_peer: str,
    serving: ServingDeviceResolution,
    observer: tailnet.Device | None,
    edge,
    firewall=None,
) -> tuple[Layer, ...]:
    """The independent things that each had to hold, outermost first.

    Ordered the way a request encounters them rather than by importance, so
    reading down the list is reading the path in. Depth is the point: no single
    one of these is the reason HQ is not on the internet.
    """

    found = [
        _name_layer(request),
        _observed_control_layer(edge),
        _channel_layer(address, channel),
        # Placed against the address claim above, because that is what it
        # qualifies: the previous line says the address is on the tailnet,
        # and this says whether the packet had to arrive there to be let in.
        _arrival_layer(firewall),
        *_policy_layers(device, forwarder, serves, request, known, forwarded),
        _device_layer(device),
        # Under the device claim, because it is what that claim rests on: the
        # line above says the tailnet knows this node, and this says whether the
        # tailnet had to be shown a signature to believe it.
        _lock_layer(device),
        _identity_agreement_layer(identity),
        _forwarder_layer(
            forwarder,
            trusted=forwarding_trusted,
            peer=forwarding_peer,
        )
        if forwarded
        else None,
        _proxy_headers_layer(
            request,
            trusted=forwarding_trusted,
            address=address,
        )
        if forwarded
        else None,
        _tailnet_observation_layer(observer, serving),
        _gate_layer(channel),
        _sign_in_layer(identity),
        _session_layer(request, identity),
        _transport_layer(request, channel),
        _canonical_layer(),
        _browser_layer(),
    ]
    return tuple(layer for layer in found if layer is not None)


def _observed_control_layer(control) -> Layer | None:
    """Project a shared provider control into the request decision timeline."""

    if control is None:
        return None
    conclusive = control.state != "neutral"
    return Layer(
        control.id,
        control.label,
        control.state == "good",
        control.detail,
        evidence=control.evidence,
        boundary="Reverse proxy edge",
        mechanism="The proxy's access list",
        conclusive=conclusive,
    )


def _name_layer(request) -> Layer:
    """Whether the name in the address bar is one the internet can resolve."""

    from .zones import public_answers_for

    host, _port = split_host_port(request.get_host())
    answers = public_answers_for(host)
    return Layer(
        "name",
        "The name is not in public DNS",
        not answers,
        (
            "No public DNS record exists for this name in any domain HQ reads."
            if not answers
            else "A public DNS record exists for this name."
        ),
        evidence=host if not answers else f"{host} → {', '.join(answers)}",
        boundary="Exposure",
        mechanism="Public DNS records",
    )


def _arrival_layer(firewall) -> Layer | None:
    """Whether the address above was required, or only presented.

    The channel layer reads an address, and an address is a field the sender
    writes. This reads the host firewall's own rule for HQ's port: accepted only
    from packets that arrived on the tailnet interface, which is not a field and
    cannot be set from somewhere else.

    Absent when nothing observed it: on a machine that does not run this
    firewall there is no reading to project, and inventing a verdict from that
    silence is the failure this page is built to avoid.
    """

    if firewall is None:
        return None
    return Layer(
        "arrival",
        firewall.label,
        firewall.state == "good",
        firewall.detail,
        evidence=firewall.evidence,
        boundary="Network",
        mechanism="The firewall rule",
        # "neutral" is the reading that never came, and that is neither a pass
        # nor a denial: an unobserved firewall must not read as an open one.
        conclusive=firewall.state != "neutral",
    )


def _channel_layer(address: str, channel: Channel) -> Layer:
    return Layer(
        "channel",
        "The address is on the tailnet",
        channel.id == "tailnet",
        channel.detail,
        evidence=address,
        boundary="Network",
        mechanism="The tailnet address range",
    )


def _policy_layers(
    device: tailnet.Device | None,
    forwarder: tailnet.Device | None,
    serves: tailnet.Device | None,
    request,
    known: dict[str, tailnet.Device],
    forwarded: bool,
) -> tuple[Layer, ...]:
    """Every independently authorized Tailnet segment in this request path.

    Being on the tailnet is not being allowed to reach everything on it, and
    that distinction is the one people assume away. Answered by Tailscale
    during the sweep rather than worked out here, for the reason given in
    ``application.tailnet``: a second implementation of an access policy is
    believed exactly as much as the real one and wrong where nobody looks.
    """

    if not forwarded:
        layer = _policy_layer(
            "policy",
            "The tailnet policy allows this device",
            device,
            serves,
            _port_of(request),
            known,
        )
        return (layer,) if layer else ()

    if forwarder is None:
        # A reverse proxy on the same host forwards from a loopback address,
        # which the tailnet never sees and cannot have a grant about. Asked as
        # "device -> forwarder" the question has no target, so both layers
        # returned nothing and the whole boundary disappeared from the page,
        # on the deployment where it matters most.
        #
        # The hop the policy actually governs is the one the caller dialled:
        # their device to the node HQ runs on, on the port they connected to.
        # That is one tailnet hop, so it is one layer. Claiming a second for
        # proxy-to-HQ would describe a loopback socket as a policed crossing.
        layer = _policy_layer(
            "policy",
            "The tailnet policy allows this device",
            device,
            serves,
            443 if request.is_secure() else 80,
            known,
        )
        return (layer,) if layer else ()

    edge = _policy_layer(
        "edge-policy",
        "The tailnet policy allows you to reach the proxy",
        device,
        forwarder,
        443 if request.is_secure() else 80,
        known,
    )
    service = _policy_layer(
        "service-policy",
        "The tailnet policy allows the proxy to reach HQ",
        forwarder,
        serves,
        _server_port(request),
        known,
    )
    return tuple(layer for layer in (edge, service) if layer is not None)


def _policy_layer(
    layer_id: str,
    label: str,
    source: tailnet.Device | None,
    target: tailnet.Device | None,
    port: int,
    known: dict[str, tailnet.Device],
) -> Layer | None:
    if source is None or target is None:
        missing = "your device" if source is None else "HQ's machine"
        return Layer(
            layer_id,
            label,
            False,
            f"HQ could not find {missing} on the tailnet, so it cannot check "
            "the policy for this step.",
            evidence=f"port {port} · {missing} not found",
            boundary="Zero trust policy",
            mechanism="The tailnet policy",
            conclusive=False,
        )
    # Names for the lookup, labels for the sentence: the policy is keyed on the
    # node's registered name, but a person reads the MagicDNS label, and two
    # phones registered as "localhost" are indistinguishable in the other one.
    verdict = tailnet.may_reach(source.name, target.name, port, known)
    owners = tailnet.alias_owners(known)
    if not verdict.known:
        return Layer(
            layer_id,
            label,
            False,
            verdict.detail,
            evidence=f"{source.label} → {target.label} on {port}",
            boundary="Zero trust policy",
            mechanism="The tailnet policy",
            conclusive=False,
        )
    return Layer(
        layer_id,
        label,
        verdict.allowed,
        verdict.detail,
        evidence=", ".join(tailnet.as_devices(verdict.via, owners)) or f"port {port}",
        # The grant itself. "Allowed" without the rule that allowed it is a
        # verdict nobody can check, and the rule is the thing an operator would
        # go and change, so the answer carries it rather than pointing at a
        # policy document and wishing them luck.
        rules=tuple(
            f"{', '.join(tailnet.as_devices(rule.get('who') or ['?'], owners))} → "
            f"{'; '.join(tailnet.by_device(rule.get('to') or ['?'], owners))}"
            for rule in verdict.rules
        ),
        boundary="Zero trust policy",
        mechanism="The tailnet policy",
    )


def _port_of(request) -> int:
    _host, port = split_host_port(request.get_host())
    if port.isdigit():
        return int(port)
    return 443 if request.is_secure() else 80


def _server_port(request) -> int:
    """The port the forwarding peer actually reached on HQ."""

    value = str(request.META.get("SERVER_PORT", "") or "")
    return int(value) if value.isdigit() else _port_of(request)


def _device_layer(device: tailnet.Device | None) -> Layer:
    if device is None:
        return Layer(
            "device",
            "The tailnet knows this device",
            False,
            "No device on the tailnet has this address.",
            boundary="Device identity",
            mechanism="The tailnet device list",
        )
    # Being in the inventory means the tailnet has seen this machine. It does
    # not mean the tailnet will carry traffic for it. A device pending approval
    # is listed and admitted to nothing, and under tailnet lock a node whose key
    # no signing node has signed is filtered out by every peer while reporting
    # itself as healthy. Both are checked here, beyond "is this a node".
    if not device.authorized:
        return Layer(
            "device",
            "The tailnet knows this device",
            False,
            f"{device.label} is on the tailnet but has not been approved.",
            evidence=device.dns_name or device.name,
            boundary="Device identity",
            mechanism="Tailnet device approval",
        )
    if device.lock_error:
        return Layer(
            "device",
            "The tailnet knows this device",
            False,
            f"{device.label} is not signed for tailnet lock, so other devices "
            f"ignore it. {device.lock_error}",
            evidence=device.dns_name or device.name,
            boundary="Device identity",
            mechanism="Tailnet lock signature",
        )
    expiry = (
        f" Its node key {_expiry_phrase(device.key_expires)}."
        if device.key_expires
        else " Its node key does not expire."
    )
    return Layer(
        "device",
        "The tailnet knows this device",
        True,
        f"{device.label} is on the tailnet, owned by "
        f"{device.user or 'no reported owner'}, and approved."
        f"{expiry}",
        evidence=device.dns_name or device.name,
        boundary="Device identity",
        mechanism="The tailnet device list",
    )


def _lock_layer(device: tailnet.Device | None) -> Layer | None:
    """Whether this node's key was signed, or merely handed out.

    Every layer above this one ultimately trusts that the coordination server
    distributed the right public key for this node. Tailnet lock is the only
    thing that removes that trust: with it on, a node key is filtered by every
    peer unless a signing key vouches for it, and the coordination server does
    not hold the signing keys. So this is not another check on the device: it
    is the check on the thing the device check was believing.
    """

    if device is None:
        # The layer above already said no node answers at this address. A
        # second line about that node's signature would be describing a device
        # that does not exist.
        return None
    lock = tailnet.policy().lock
    if not lock:
        # No reading at all, which is not the same as lock being off. A tailnet
        # nothing has swept should say nothing here rather than render a line
        # about a setting HQ has not looked at.
        return None
    keys = lock.get("trusted_keys") or 0
    if not lock.get("enabled"):
        return Layer(
            "tailnet-lock",
            "Node key signed by tailnet lock",
            False,
            "Tailnet lock is off, so node keys are not signed.",
            evidence="Lock off",
            boundary="Device identity",
            mechanism="Tailnet lock status",
            conclusive=False,
        )
    if device.lock_error:
        return Layer(
            "tailnet-lock",
            "Node key signed by tailnet lock",
            False,
            f"{device.label} has no valid tailnet lock signature, so other "
            f"devices ignore it. {device.lock_error}",
            evidence="Unsigned",
            boundary="Device identity",
            mechanism="Tailnet lock signature",
        )
    return Layer(
        "tailnet-lock",
        "Node key signed by tailnet lock",
        True,
        f"Tailnet lock is on and {device.label}'s node key has a valid signature.",
        evidence=f"Signed · {counted(keys, 'signing key', 'signing keys')}",
        boundary="Device identity",
        mechanism="Tailnet lock signature",
    )


def _identity_agreement_layer(identity: Identity) -> Layer:
    """Compare the independently observed device owner and signed-in person."""

    if not identity.tailnet_user:
        return Layer(
            "identity-agreement",
            "The device's owner is the person signed in",
            False,
            "The tailnet reports no owner for this device, so HQ cannot compare "
            "it with the person signed in.",
            evidence="No device owner reported",
            boundary="Identity correlation",
            mechanism="The session and the tailnet device owner",
            conclusive=False,
        )
    session_principal = identity.email or identity.username
    if not identity.corroborated and not identity.conflicted:
        return Layer(
            "identity-agreement",
            "The device's owner is the person signed in",
            False,
            "The device's tailnet owner and the HQ account are two separate "
            "names, and single sign-on did not link them.",
            evidence=f"{identity.tailnet_user} ↔ {session_principal or 'unnamed session'}",
            boundary="Identity correlation",
            mechanism="The session and the tailnet device owner",
            conclusive=False,
        )
    return Layer(
        "identity-agreement",
        "The device's owner is the person signed in",
        identity.corroborated,
        (
            "Single sign-on put your Tailscale account in this session, and "
            "Tailscale reports the same account as this device's owner."
            if identity.agreement_basis == "SSO-signed principal link"
            else "The tailnet account that owns this device is the account "
            "signed in to HQ."
            if identity.corroborated
            else "The tailnet account that owns this device is a different "
            "account from the one signed in. Check who owns the device and "
            "who is signed in."
        ),
        evidence=(
            f"Device owner {identity.tailnet_user} · single sign-on "
            f"{identity.provider_principal} · signed in as "
            f"{session_principal or 'unnamed session'}"
            if identity.agreement_basis == "SSO-signed principal link"
            else f"{identity.tailnet_user} ↔ "
            f"{session_principal or 'unnamed session'}"
        ),
        boundary="Identity correlation",
        mechanism=(
            "Single sign-on and the tailnet device owner"
            if identity.agreement_basis == "SSO-signed principal link"
            else "The session and the tailnet device owner"
        ),
    )


def _forwarder_layer(
    device: tailnet.Device | None, *, trusted: bool, peer: str
) -> Layer:
    if not trusted:
        return Layer(
            "forwarder",
            "HQ trusts the proxy that forwarded this",
            False,
            "This address is not on HQ's list of trusted proxies, so HQ ignores "
            "the address it forwarded.",
            evidence=peer,
            boundary="Forwarding identity",
            mechanism="HQ's list of trusted proxies",
        )
    if device is None:
        return Layer(
            "forwarder",
            "HQ trusts the proxy that forwarded this",
            True,
            "This address is on HQ's list of trusted proxies.",
            evidence=peer,
            boundary="Forwarding identity",
            mechanism="HQ's list of trusted proxies",
        )
    return Layer(
        "forwarder",
        "HQ trusts the proxy that forwarded this",
        True,
        f"{device.label} holds this address, which is on HQ's list of trusted "
        "proxies.",
        evidence=device.dns_name or device.name,
        boundary="Forwarding identity",
        mechanism="HQ's list of trusted proxies and the tailnet device list",
    )


def _proxy_headers_layer(request, *, trusted: bool, address: str) -> Layer | None:
    """Cross-check a proxy's redundant forwarding headers without granting them authority.

    The headers are the ones a proxy kind declares as ``forwarding_headers``.
    """

    if not trusted:
        return None
    from hq.domains.control_plane.connection_kinds import CONNECTION_LABELS
    from hq.domains.control_plane.providers import PROVIDERS

    declared = next(
        (spec for spec in PROVIDERS.values() if spec.forwarding_headers), None
    )
    if declared is None:
        return None
    client_header, scheme_header = declared.forwarding_headers
    proxy = next(
        (
            CONNECTION_LABELS[name]
            for name in declared.connection_providers
            if name in CONNECTION_LABELS
        ),
        declared.label,
    )
    mechanism = f"{proxy} forwarding headers"

    def header(name: str) -> str:
        return str(request.META.get(f"HTTP_{name.upper().replace('-', '_')}", "") or "").strip()

    real = header(client_header)
    scheme = header(scheme_header).lower()
    if not real or not scheme:
        return Layer(
            "proxy-evidence",
            "The proxy's headers agree",
            False,
            f"The proxy did not send both {client_header} and {scheme_header}, "
            "so HQ cannot compare them.",
            evidence="Headers incomplete",
            boundary="Forwarding evidence",
            mechanism=mechanism,
            conclusive=False,
        )
    real_host = split_host_port(real)[0]
    expected_scheme = "https" if request.is_secure() else "http"
    agrees = real_host == address and scheme == expected_scheme
    return Layer(
        "proxy-evidence",
        "The proxy's headers agree",
        agrees,
        (
            f"{proxy}'s {client_header} and {scheme_header} headers match the "
            "address and scheme HQ used."
            if agrees
            else f"{proxy}'s {client_header} or {scheme_header} header differs "
            "from the address or scheme HQ used. Check the proxy's forwarding "
            "settings."
        ),
        evidence=(
            f"{client_header}={real_host or 'missing'} · "
            f"{scheme_header}={scheme or 'missing'}"
        ),
        boundary="Forwarding evidence",
        mechanism=mechanism,
    )


def _tailnet_observation_layer(
    observer: tailnet.Device | None, serving: ServingDeviceResolution
) -> Layer:
    """Say exactly whether observer-relative link data is evidence about HQ."""

    if observer is None:
        return Layer(
            "tailnet-observer",
            "HQ's own machine measured this link",
            False,
            "The tailnet reading does not say which machine measured this link.",
            boundary="Transport attestation",
            mechanism="Tailscale on the machine that read the tailnet",
            conclusive=False,
        )
    if not serving.verified:
        return Layer(
            "tailnet-observer",
            "HQ's own machine measured this link",
            False,
            "HQ could not tell which tailnet machine it runs on, so it cannot "
            "say these measurements are its own.",
            evidence=f"Measured by {observer.label} · HQ's machine not found",
            boundary="Transport attestation",
            mechanism="Tailscale on the machine that read the tailnet",
            conclusive=False,
        )
    same = bool(serving.device and observer.name == serving.device.name)
    return Layer(
        "tailnet-observer",
        "HQ's own machine measured this link",
        same,
        (
            "The machine that measured this link is the machine HQ runs on."
            if same
            else "A different machine measured this link, so the measurements "
            "do not describe your connection to HQ."
        ),
        evidence=(
            f"{observer.label} · {serving.basis}"
            if same
            else f"Measured by {observer.label} · HQ runs on {serving.device.label}"
        ),
        boundary="Transport attestation",
        mechanism="Tailscale on the machine that read the tailnet",
    )


def _expiry_phrase(stamp: str) -> str:
    """When a node key runs out, in the tense that fits.

    An expired key is not a smaller version of a valid one (the device stops
    being on the tailnet) so the two do not share a sentence.
    """

    parsed = moment(stamp)
    if parsed is None:
        return "has an expiry HQ could not read"
    from .expiry import days_until

    days = days_until(parsed)
    if days < 0:
        return f"expired {abs(days)} days ago"
    return f"expires in {days} days"


def _gate_layer(channel: Channel) -> Layer:
    """Whether HQ refuses everything else before it authenticates anything."""

    enforced = bool(getattr(settings, "SEVERINO_ENFORCE_TRUSTED_NETWORK", False))
    if enforced and channel.id == "opaque":
        return Layer(
            "gate",
            "Other networks are refused",
            False,
            "The proxy did not pass your address on, so HQ is checking the "
            "proxy's address. Anyone who can reach the proxy passes.",
            evidence="Checking a proxy",
            boundary="Application edge",
            mechanism="HQ's trusted-network setting",
        )
    return Layer(
        "gate",
        "Other networks are refused",
        enforced and channel.private,
        (
            "HQ refuses any address outside its private ranges before sign-in."
            if enforced
            else "HQ is not checking which address a request comes from."
        ),
        evidence="Enforced" if enforced else "Not enforced",
        boundary="Application edge",
        mechanism="HQ's trusted-network setting",
    )


def _sign_in_layer(identity: Identity) -> Layer:
    return Layer(
        "sign-in",
        "Sign-in is single sign-on only",
        identity.sso_only,
        (
            "Signing in goes through single sign-on, and no password sign-in "
            "is installed."
            if identity.sso_only
            else "Password sign-in is installed."
        ),
        evidence=(
            "Single sign-on only" if identity.sso_only else "Password sign-in installed"
        ),
        boundary="Human identity",
        mechanism="HQ's sign-in settings",
    )


def _session_layer(request, identity: Identity) -> Layer:
    """Whether the cookie carrying this session can leave where it was set."""

    secure = bool(getattr(settings, "SESSION_COOKIE_SECURE", False))
    http_only = bool(getattr(settings, "SESSION_COOKIE_HTTPONLY", False))
    same_site = str(getattr(settings, "SESSION_COOKIE_SAMESITE", "") or "")
    name = str(getattr(settings, "SESSION_COOKIE_NAME", "") or "")
    # The `__Host-` prefix is the only one of these the browser enforces
    # against somebody else. The other three describe the cookie HQ set; this
    # one is a promise no sibling host can have written it.
    prefixed = name.startswith("__Host-")
    holds = secure and http_only and bool(same_site) and prefixed
    stated = [
        f"Secure={'on' if secure else 'off'}",
        f"HttpOnly={'on' if http_only else 'off'}",
        f"SameSite={same_site or 'unset'}",
        name or "unnamed",
    ]
    return Layer(
        "session",
        "Session cookie is locked to this site",
        holds,
        (
            "The session cookie is sent only over HTTPS, cannot be read by "
            "scripts, is not sent with requests other sites start, and can "
            "only be set by this exact host."
            if holds
            else "The session cookie is missing a protection. Check Secure, "
            "HttpOnly, SameSite and the __Host- prefix."
        ),
        evidence=" · ".join(stated),
        boundary="Session",
        mechanism="HQ's cookie settings",
    )


def _browser_layer() -> Layer:
    """What HQ tells the browser it is allowed to do with this page.

    Every other layer on this page is about reaching HQ. This one is about what
    happens after: the page is the last place a credential is held, and the
    policy is the only boundary HQ cannot check from the inside: it is
    enforced in someone else's browser, and a directive that has quietly
    stopped applying looks exactly like one that is quietly working. So the
    policy is stated here, and violations are reported back.
    """

    policy = dict(getattr(settings, "SECURE_CSP", {}) or {})
    trusted_types = "'script'" in (policy.get("require-trusted-types-for") or ())
    scripts = tuple(policy.get("script-src") or ())
    nonce_only = CSP.NONCE in scripts and CSP.UNSAFE_INLINE not in scripts
    framed = tuple(policy.get("frame-ancestors") or ()) == (CSP.NONE,)
    reports = bool(policy.get("report-uri") or policy.get("report-to"))
    holds = trusted_types and nonce_only and framed
    stated = [
        "Trusted Types" if trusted_types else "no Trusted Types",
        "nonce scripts" if nonce_only else "inline scripts",
        "no framing" if framed else "framing allowed",
        "reported" if reports else "unreported",
    ]
    return Layer(
        "browser",
        "Scripts only from HQ",
        holds,
        (
            "Scripts run only from HQ or with a one-time code for this page, "
            "the page cannot be framed, and Trusted Types is on."
            if holds
            else "The content security policy is missing a rule that stops "
            "scripts HQ did not send."
        ),
        evidence=" · ".join(stated),
        boundary="Browser boundary",
        mechanism="Content Security Policy",
    )


def _canonical_layer() -> Layer:
    """Whether plain HTTP is a way in, and whether the browser is told it isn't.

    HQ binds a plain port and a proxy terminates TLS in front of it, so "the
    site is HTTPS" is a fact about the proxy rather than about HQ. Two things
    make it a fact about HQ: refusing a request that did not arrive as HTTPS,
    and telling the browser never to try plain HTTP for this name again.
    """

    redirected = bool(getattr(settings, "SECURE_SSL_REDIRECT", False))
    hsts = int(getattr(settings, "SECURE_HSTS_SECONDS", 0) or 0)
    subdomains = bool(getattr(settings, "SECURE_HSTS_INCLUDE_SUBDOMAINS", False))
    holds = redirected and hsts > 0
    stated = [
        "plain HTTP redirected" if redirected else "plain HTTP served",
        f"HSTS {hsts // 86400} days" if hsts else "HSTS not sent",
    ]
    if hsts and subdomains:
        stated.append("subdomains included")
    return Layer(
        "canonical",
        "HTTPS only",
        holds,
        (
            "HQ redirects plain HTTP to HTTPS and tells the browser to use "
            "HTTPS only for this name."
            if holds
            else "HQ serves plain HTTP on its port."
            if not redirected
            else "HQ redirects plain HTTP but sends no HSTS header, so the "
            "browser's first request to this name can still be plain HTTP."
        ),
        evidence=" · ".join(stated),
        boundary="Transport",
        mechanism="HTTPS redirect and HSTS",
    )


def _transport_layer(request, channel: Channel) -> Layer:
    tls = bool(request.is_secure())
    tailnet = channel.id == "tailnet"
    if tailnet and tls:
        detail = (
            "WireGuard encrypts the link between the two machines, and TLS "
            "encrypts this request inside it."
        )
        evidence = "WireGuard + TLS"
    elif tailnet:
        detail = "WireGuard encrypts this request. There is no TLS inside it."
        evidence = "WireGuard only"
    elif tls:
        detail = "TLS encrypts this request. The caller is not on the tailnet."
        evidence = "TLS"
    else:
        detail = "HQ cannot confirm this request was encrypted."
        evidence = "Encryption not confirmed"
    return Layer(
        "transport",
        "The connection is encrypted",
        tls or tailnet,
        detail,
        evidence=evidence,
        boundary="Transport",
        mechanism="WireGuard and TLS",
    )

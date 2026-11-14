"""The request headers HQ reads, the ones it declines to, and how each is shown."""

from dataclasses import dataclass

# What HQ did with a header, as the tone its pill takes.
_STATE_TONES = {"read": "good", "declined": "attention", "ignored": "neutral"}


@dataclass(frozen=True, slots=True)
class Header:
    """One header as it arrived, and what HQ did with it."""

    name: str
    value: str
    purpose: str = ""
    # Why this one is not believed, where declining it was a decision rather
    # than an absence.
    declined: str = ""
    redacted: bool = False

    @property
    def used(self) -> bool:
        return bool(self.purpose)

    @property
    def state(self) -> str:
        if self.purpose:
            return "read"
        return "declined" if self.declined else "ignored"

    @property
    def tone(self) -> str:
        """Read reads as good, declined as a choice to look at, ignored as neither."""

        return _STATE_TONES[self.state]


# What each header HQ reads is read *for*. Written here rather than inferred,
# because the thing being described is what the code does with it, and a
# header nobody reads has no entry, which is the point of the second list.
HEADERS_READ = {
    "Host": "Checked against HQ's allowed names.",
    "X-Forwarded-For": "Your address, from a trusted proxy.",
    "X-Forwarded-Proto": "Whether the request reached the proxy over HTTPS.",
    "Origin": "Checked against the sites allowed to send forms.",
    "Referer": "Checked when Origin is absent.",
    "Cookie": "Your session.",
}
REDACTED = {"Cookie", "Authorization", "X-Csrftoken", "Proxy-Authorization"}
# A proxy or an identity-aware gateway in front of HQ can add credentials of
# its own, under names nobody listed here. A name that says what it carries is
# enough to hide it.
_CREDENTIAL_WORDS = ("auth", "token", "secret", "key", "jwt", "assertion", "session", "cookie", "password")


def redacted(value: str) -> str:
    """How a header HQ never shows is shown: that it came, and its length."""

    return f"present, {len(value)} characters"


def is_redacted(name: str) -> bool:
    lowered = name.lower()
    return name in REDACTED or any(word in lowered for word in _CREDENTIAL_WORDS)


# Headers deliberately not believed, and the reason. Without these the page
# lists a header carrying the correct answer as merely ignored, which reads as
# an oversight rather than as the safer of two choices.
HEADERS_DECLINED = {
    "X-Real-Ip": "HQ reads X-Forwarded-For.",
    "X-Forwarded-Scheme": "HQ reads X-Forwarded-Proto.",
    "X-Forwarded-Host": "HQ reads Host.",
}


@dataclass(frozen=True, slots=True)
class ResponseCheck:
    """One thing the browser reads back from HQ's own response: a header, or a
    directive of the content security policy, and what it is for."""

    name: str
    purpose: str
    directive: bool = False


# What HQ sends back, read by the browser from a real response rather than
# from the settings meant to produce one: a same-origin fetch exposes every
# response header. The directives are the policy's parts worth naming, since
# the policy is one long header whose most consequential parts a reader would
# never spot in it; a directive dropped in configuration shows as absent.
RESPONSE_CHECKS = (
    ResponseCheck("x-served-by", "Which reverse-proxy hostname actually served this response"),
    ResponseCheck("x-request-id", "Joins this browser response to HQ's structured request log"),
    ResponseCheck("content-security-policy", "Which origins may load script, style and frames"),
    ResponseCheck("strict-transport-security", "Refuses plain HTTP for this host from now on"),
    ResponseCheck("x-content-type-options", "Stops the browser guessing a type it was not sent"),
    ResponseCheck("x-frame-options", "Refuses to be framed by another site"),
    ResponseCheck("referrer-policy", "How much of this URL travels to anywhere you click"),
    ResponseCheck("cross-origin-opener-policy", "Keeps other origins out of this browsing context"),
    ResponseCheck("cross-origin-resource-policy", "Refuses to be loaded as a subresource elsewhere"),
    ResponseCheck("permissions-policy", "Which device features this page may ask for"),
    ResponseCheck("reporting-endpoints", "Where the browser sends a policy it refused to follow"),
    ResponseCheck(
        "require-trusted-types-for", "Assigning a string to a DOM sink throws instead of parsing", directive=True
    ),
    ResponseCheck("frame-ancestors 'none'", "Nothing may frame this page", directive=True),
    ResponseCheck("object-src 'none'", "No plugins or embedded objects", directive=True),
    ResponseCheck("base-uri 'self'", "Injected markup cannot re-point every relative URL", directive=True),
    ResponseCheck("form-action 'self'", "A form cannot be made to submit somewhere else", directive=True),
)


def headers_of(request) -> tuple[Header, ...]:
    """Every header this request carried, with the ones HQ acts on first.

    The raw input to every decision on this page. A value HQ reads is worth
    seeing beside the conclusion drawn from it, and the ones it does *not*
    read are worth seeing too, because "the proxy is sending X-Real-IP and
    nothing here looks at it" is invisible until somebody prints both lists.
    """

    found: list[Header] = []
    for name, value in sorted(request.headers.items()):
        # A server sets Content-Type and Content-Length on every request, empty
        # when the request sent none, so an empty value is a header never carried.
        if value == "":
            continue
        hidden = is_redacted(name)
        found.append(
            Header(
                name=name,
                value=redacted(str(value)) if hidden else str(value),
                purpose=HEADERS_READ.get(name, ""),
                declined=HEADERS_DECLINED.get(name, ""),
                redacted=hidden,
            )
        )
    # Acted on first, then deliberately declined, then everything else.
    order = {"read": 0, "declined": 1, "ignored": 2}
    return tuple(sorted(found, key=lambda header: order[header.state]))

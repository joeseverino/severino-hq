"""The request headers HQ reads, the ones it declines to, and how each is shown."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
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


# What each header HQ reads is read *for*. Written here rather than inferred,
# because the thing being described is what the code does with it, and a
# header nobody reads has no entry, which is the point of the second list.
HEADERS_READ = {
    "Host": "Which site this is, checked against the hosts HQ will answer for.",
    "X-Forwarded-For": "The chain HQ walks to decide the address it judges you by.",
    "X-Forwarded-Proto": "Whether the proxy terminated TLS, so HQ knows the request was encrypted.",
    "Origin": "Checked against the origins allowed to submit forms here.",
    "Referer": "The same check, for browsers that send this instead.",
    "Cookie": "Carries the session. Its contents are never shown, here or anywhere.",
}
REDACTED = {"Cookie", "Authorization", "X-Csrftoken", "Proxy-Authorization"}
# A proxy or an identity-aware gateway in front of HQ can add credentials of
# its own, under names nobody listed here. A name that says what it carries is
# enough to hide it.
_CREDENTIAL_WORDS = ("auth", "token", "secret", "key", "jwt", "assertion", "session", "cookie", "password")


def is_redacted(name: str) -> bool:
    lowered = name.lower()
    return name in REDACTED or any(word in lowered for word in _CREDENTIAL_WORDS)
# Headers deliberately not believed, and the reason. Without these the page
# lists a header carrying the correct answer as merely ignored, which reads as
# an oversight rather than as the safer of two choices.
HEADERS_DECLINED = {
    "X-Real-Ip": (
        "Carries one address the proxy asserts, with no chain behind it to "
        "check. HQ reads the forwarded chain instead, which it can walk back "
        "through the proxies it knows and stop at the first hop it cannot "
        "vouch for. Believing a single asserted value would be weaker."
    ),
    "X-Forwarded-Scheme": (
        "Says the same thing as X-Forwarded-Proto, which is the one Django is "
        "configured to read. Two sources for one fact is one more than can be "
        "trusted to agree."
    ),
    "X-Forwarded-Host": (
        "The host is taken from the request line and checked against the hosts "
        "HQ will answer for. A forwarded copy could disagree with it."
    ),
}


def headers_of(request) -> tuple[Header, ...]:
    """Every header this request carried, with the ones HQ acts on first.

    The raw input to every decision on this page. A value HQ reads is worth
    seeing beside the conclusion drawn from it, and the ones it does *not*
    read are worth seeing too, because "the proxy is sending X-Real-IP and
    nothing here looks at it" is invisible until somebody prints both lists.
    """

    found: list[Header] = []
    for key, value in sorted(request.META.items()):
        if key == "HTTP_HOST":
            name = "Host"
        elif key.startswith("HTTP_"):
            name = key[5:].replace("_", "-").title()
        else:
            continue
        redacted = is_redacted(name)
        found.append(
            Header(
                name=name,
                value=(
                    f"present, {len(str(value))} characters"
                    if redacted
                    else str(value)
                ),
                purpose=HEADERS_READ.get(name, ""),
                declined=HEADERS_DECLINED.get(name, ""),
                redacted=redacted,
            )
        )
    # Acted on first, then deliberately declined, then everything else.
    order = {"read": 0, "declined": 1, "ignored": 2}
    return tuple(sorted(found, key=lambda header: order[header.state]))

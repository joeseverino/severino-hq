"""The request headers HQ reads, the ones it declines to, and how each is shown."""

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
    "Host": "The name asked for. HQ answers only for names on its list.",
    "X-Forwarded-For": "The addresses this request passed through. HQ takes yours from it.",
    "X-Forwarded-Proto": "Whether the request reached the proxy over HTTPS.",
    "Origin": "The site a form was sent from, checked against the sites allowed to send forms to HQ.",
    "Referer": "The page a form was sent from, used when Origin is absent.",
    "Cookie": "Holds your session. HQ never shows its contents.",
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
        "One address from the proxy. HQ takes your address from "
        "X-Forwarded-For instead."
    ),
    "X-Forwarded-Scheme": (
        "Repeats X-Forwarded-Proto, which is the one HQ reads."
    ),
    "X-Forwarded-Host": (
        "HQ takes the name from the Host header and ignores this copy."
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

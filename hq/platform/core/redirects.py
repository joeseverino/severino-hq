"""Where an address of HQ's lands, each redirect to another page of HQ's followed as a browser
follows it. Its imports are lazy, so the memory probe loads it before its count at no cost to HQ."""

lazy from collections.abc import Callable
lazy from http import HTTPStatus
lazy from typing import Any
lazy from urllib.parse import urljoin, urlsplit

# More than the longest chain of redirects within HQ a page takes.
HOPS = 5


def _response(answer: Any) -> tuple[int, str]:
    return answer.status_code, answer.get("Location", "")


def follow[T](
    ask: Callable[[str], T], address: str, read: Callable[[T], tuple[int, str]] = _response
) -> tuple[str, T, str]:
    """The last address asked, its answer, and where that answer redirects when it was not
    followed: out of HQ, back to an address already asked, or past ``HOPS``; "" once it lands.
    ``read`` gives an answer's status and Location header, a Django response's by default."""

    seen = [address]
    while True:
        status, location = read(answer := ask(address))
        if not HTTPStatus(status).is_redirection or not location:
            return address, answer, ""
        to = urlsplit(urljoin(address, location))
        if to.scheme or to.netloc or (next_address := to._replace(fragment="").geturl()) in seen or len(seen) > HOPS:
            return address, answer, to.geturl()
        seen.append(address := next_address)

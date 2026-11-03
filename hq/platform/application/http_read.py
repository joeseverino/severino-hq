"""One bounded read over HTTP, as each of HQ's outside readers makes it.

httpx2 follows a redirect only when asked, and on one it follows it drops
``Authorization`` once the origin changes. A reader whose rule for a redirect
is stricter (a public address only, one origin only), or whose credential is
another header, holds it in an event hook (``redirect_target``).
"""

from collections.abc import Callable, Iterator, Mapping

import httpx2

# What a read that never got an answer raises. httpx2 maps a socket's own
# errors to its own; OSError stays for a system error raised beneath it.
UNREACHABLE = (httpx2.HTTPError, httpx2.InvalidURL, httpx2.StreamError, OSError)

Hook = Callable[..., None]


def at_most(chunks: Iterator[bytes], limit: int) -> bytes:
    """The body's first ``limit`` bytes, reading no further than it takes."""

    body = b""
    for chunk in chunks:
        body += chunk
        if len(body) >= limit:
            return body[:limit]
    return body


def redirect_target(response: httpx2.Response) -> httpx2.URL | None:
    """Where ``response`` redirects to, for a response hook to judge before
    httpx2 follows it, or None when it is not a redirect."""

    if not response.has_redirect_location:
        return None
    return response.url.join(response.headers["Location"])


def fetch(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    timeout: float,
    limit: int,
    content: bytes | None = None,
    follow_redirects: bool = False,
    on_request: tuple[Hook, ...] = (),
    on_response: tuple[Hook, ...] = (),
) -> tuple[httpx2.Response, bytes]:
    """The answer to one request, and at most ``limit + 1`` bytes of its body,
    so a caller can tell an answer that ran past ``limit``. Raises one of
    ``UNREACHABLE`` when nothing answered, and whatever a hook raises."""

    hooks = {"request": list(on_request), "response": list(on_response)}
    with (
        httpx2.Client(timeout=timeout, follow_redirects=follow_redirects, event_hooks=hooks) as client,
        client.stream(method, url, headers=headers, content=content) as response,
    ):
        body = b"" if method == "HEAD" else at_most(response.iter_bytes(), limit + 1)
    return response, body

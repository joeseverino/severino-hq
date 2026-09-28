"""How the controller speaks HTTP to a provider: one request path, fail-closed."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
import secrets
import ssl
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, TypeVar, cast

from control_plane.provider_adapters.contracts import (
    ADDRESS_FAILURE,
    CREDENTIAL_REFUSAL,
    PERMISSION_REFUSAL,
    ProviderError,
    failure_of,
)

logger = logging.getLogger("severino.controller")


logger = logging.getLogger("severino.controller")

_SnapshotValue = TypeVar("_SnapshotValue")
_PROVIDER_SNAPSHOT: ContextVar[dict[tuple[object, ...], object] | None] = ContextVar(
    "provider_snapshot", default=None
)
@contextmanager
def provider_snapshot() -> Iterator[None]:
    """Share successful reads only for one logically atomic provider sweep."""

    token = _PROVIDER_SNAPSHOT.set({})
    try:
        yield
    finally:
        _PROVIDER_SNAPSHOT.reset(token)


def _snapshot_value(
    key: tuple[object, ...], load: Callable[[], _SnapshotValue]
) -> _SnapshotValue:
    snapshot = _PROVIDER_SNAPSHOT.get()
    if snapshot is None:
        return load()
    if key not in snapshot:
        snapshot[key] = load()
    return cast(_SnapshotValue, snapshot[key])


def _tls_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    ca_file = os.environ.get("HQ_CONTROLLER_CA_FILE", "").strip()
    if ca_file:
        try:
            context.load_verify_locations(cafile=ca_file)
        except (OSError, ssl.SSLError) as exc:
            raise ProviderError("Controller CA bundle could not be loaded.") from exc
    return context


def _condition(
    condition_type: str, status: bool, reason: str, message: str
) -> dict[str, Any]:
    return {
        "type": condition_type,
        "status": status,
        "reason": reason,
        "message": message,
    }


def _release(exc: BaseException) -> None:
    """Close the response a failed request carries, if it carries one.

    An ``HTTPError`` is not only an exception: it is the error response,
    socket included, and nothing closes it on our behalf. Chained into a
    ``ProviderError`` or swallowed into a default, it would hold that socket
    until the garbage collector found it: one per refused call, on a worker
    that makes dozens of them a pass. Every handler that can receive one calls
    this, so the rule is written once rather than remembered at each site. The
    other failures a request can raise own nothing, and pass through untouched.
    """

    if isinstance(exc, urllib.error.HTTPError):
        exc.close()


_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_DIRECT_ADDRESS = "Use the provider's direct API address."


def _json_answer(url: str, response: Any, raw: bytes) -> Any:
    """The JSON a provider answered, or a ProviderError saying what answered instead.

    A URL behind a sign-in proxy is redirected to a login page and answers
    HTML. Only the redirect's host is named: its query string carries client
    IDs and state.
    """

    asked = (urllib.parse.urlsplit(url).hostname or "").lower()
    final = response.geturl() if callable(getattr(response, "geturl", None)) else ""
    landed = (
        (urllib.parse.urlsplit(final).hostname or "").lower()
        if isinstance(final, str)
        else ""
    )
    headers = getattr(response, "headers", None)
    content_type = headers.get_content_type() if hasattr(headers, "get_content_type") else ""
    html = (isinstance(content_type, str) and content_type in _HTML_TYPES) or (
        isinstance(raw, bytes) and raw.lstrip()[:1] == b"<"
    )
    if landed and asked and landed != asked:
        raise ProviderError(
            f"The address answered with a sign-in page at {landed}, not the API. "
            f"{_DIRECT_ADDRESS}"
            if html
            else f"The address redirected to {landed}, not the API. {_DIRECT_ADDRESS}",
            failure=ADDRESS_FAILURE,
        )
    if html:
        raise ProviderError(
            f"The address answered with a web page, not the API. {_DIRECT_ADDRESS}",
            failure=ADDRESS_FAILURE,
        )
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderError("Provider returned invalid JSON.") from exc


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    try:
        port = parts.port
    except ValueError:
        port = -1
    return scheme, (parts.hostname or "").lower(), port or _DEFAULT_PORTS.get(scheme)


_DEFAULT_PORTS = {"http": 80, "https": 443}
_REDIRECTABLE = frozenset({"GET", "HEAD"})


class _SameOriginRedirects(dict):
    """urllib's per-request redirect ledger, refusing a hop that leaves the request.

    ``HTTPRedirectHandler`` consults ``redirect_dict`` before it follows each
    hop, so a request carrying this one refuses a redirect to another scheme,
    host or port, and any redirect of a request that is not a read, before the
    next request is sent.
    """

    def __init__(self, url: str, method: str):
        super().__init__()
        self._origin = _origin(url)
        self._method = method.upper()

    # A ledger belongs to one request, so two are equal only when they are one.
    def __eq__(self, other: object) -> bool:
        return self is other

    __hash__ = None  # type: ignore[assignment]

    def get(self, key: Any, default: Any = None) -> Any:
        if self._method not in _REDIRECTABLE:
            raise ProviderError(
                f"The address redirected a {self._method} request, which is not "
                f"followed. {_DIRECT_ADDRESS}",
                failure=ADDRESS_FAILURE,
            )
        if _origin(str(key)) != self._origin:
            landed = urllib.parse.urlsplit(str(key)).hostname or "another address"
            raise ProviderError(
                f"The address redirected to {landed}, not the API. {_DIRECT_ADDRESS}",
                failure=ADDRESS_FAILURE,
            )
        return super().get(key, default)


def _provider_request(
    url: str, *, data: bytes | None, headers: dict[str, str], method: str
) -> urllib.request.Request:
    """A request whose caller-supplied headers are not sent on to a redirect.

    urllib copies headers to wherever a redirect points, credentials included;
    unredirected headers stay with the address they were meant for. The
    request also carries ``_SameOriginRedirects``, so a redirect off its origin
    is refused rather than followed.
    """

    request = urllib.request.Request(url, data=data, method=method)
    for name, value in headers.items():
        if name.lower() in {"accept", "content-type"}:
            request.add_header(name, value)
        else:
            request.add_unredirected_header(name, value)
    request.redirect_dict = _SameOriginRedirects(url, method)  # type: ignore[attr-defined]
    return request


def _open(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = 15,
) -> Any:
    """The one way the controller sends a provider request.

    Credentials ride only as unredirected headers, a redirect off the request's
    origin is refused, and TLS is verified with ``_tls_context()``. Returns the
    open response; ``urllib.error`` exceptions propagate to the caller.
    """

    request = _provider_request(url, data=data, headers=headers or {}, method=method)
    return urllib.request.urlopen(  # noqa: S310 - URLs are deployment config.
        request, timeout=timeout, context=_tls_context()
    )


def _request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> Any:
    body = None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if payload is not None:
        body = json.dumps(payload).encode()
        request_headers["Content-Type"] = "application/json"
    try:
        with _open(
            url, data=body, headers=request_headers, method=method
        ) as response:
            raw = response.read()
            return _json_answer(url, response, raw)
    except (urllib.error.URLError, TimeoutError) as exc:
        _release(exc)
        failure = failure_of(exc)
        raise ProviderError(
            f"Provider request failed: {type(exc).__name__}.",
            refusal=failure if failure in (CREDENTIAL_REFUSAL, PERMISSION_REFUSAL) else "",
            failure=failure,
        ) from exc


def _multipart_request(
    url: str,
    *,
    headers: dict[str, str],
    files: dict[str, tuple[str, bytes]],
) -> Any:
    boundary = f"----severino-hq-{secrets.token_hex(16)}"
    chunks: list[bytes] = []
    for field, (filename, content) in files.items():
        chunks.extend(
            (
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="{field}"; '
                    f'filename="{filename}"\r\n'
                ).encode(),
                b"Content-Type: application/x-pem-file\r\n\r\n",
                content,
                b"\r\n",
            )
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    try:
        with _open(
            url,
            data=b"".join(chunks),
            headers={
                "Accept": "application/json",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                **headers,
            },
            method="POST",
            timeout=30,
        ) as response:
            raw = response.read()
            return _json_answer(url, response, raw)
    except (urllib.error.URLError, TimeoutError) as exc:
        _release(exc)
        raise ProviderError(
            f"Provider multipart request failed: {type(exc).__name__}."
        ) from exc


# What a missing setting reads as wherever an error is stored or shown. The
# variable it names stays in the controller's own log.
NOT_CONFIGURED = "A setting this needs is not configured on the controller."


def _required(prefix: str, name: str) -> str:
    value = os.environ.get(f"{prefix}_{name}", "").strip()
    if not value:
        logger.warning(
            "controller setting missing: %s_%s",
            prefix,
            name,
            extra={"event": "controller.config.missing"},
        )
        raise ProviderError(NOT_CONFIGURED)
    return value

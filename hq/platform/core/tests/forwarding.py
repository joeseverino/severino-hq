"""A request as HQ's forwarded-header middleware hands it to Django, for a test client to send."""

from asgiref.sync import async_to_sync

from hq.platform.core.network import SOCKET_PEER, ForwardedCaller


def arrived(**meta) -> dict:
    """``meta``, a request as its peer sent it, with what ForwardedCaller made of it.

    REMOTE_ADDR becomes the caller it resolved and SOCKET_PEER the peer, and the
    scheme a trusted proxy named is the request's. Read under the settings in
    force at the call, so an ``override_settings`` of the trusted proxies applies.
    """

    headers = [
        (key.removeprefix("HTTP_").lower().replace("_", "-").encode("latin-1"), str(value).encode("latin-1"))
        for key, value in meta.items()
        if key.startswith("HTTP_")
    ]
    seen: dict = {}

    async def app(scope, receive, send):
        seen.update(scope)

    scope = {"type": "http", "client": (meta.get("REMOTE_ADDR", ""), 0), "headers": headers}
    async_to_sync(ForwardedCaller(app))(scope, None, None)
    resolved = {**meta, "REMOTE_ADDR": seen["client"][0], SOCKET_PEER: seen[SOCKET_PEER]}
    if "scheme" in seen:
        resolved["wsgi.url_scheme"] = seen["scheme"]
    return resolved

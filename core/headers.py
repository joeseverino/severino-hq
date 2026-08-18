"""Response headers that keep to the ASGI contract."""


class LowercaseHeaders:
    """Lowercase response header names, which ASGI requires and Django skips.

    Django emits header names in the case they were set -- `Content-Length`,
    `Vary` -- deliberately, for clients that cannot cope with lowercase. ASGI
    says the opposite, so every middleware downstream is entitled to match on
    lowercase, and Starlette's does: setting a header it believes is absent
    appends a second one instead of replacing the first.

    On a compressed response that is fatal. `Content-Length` arrives
    capitalised, gzip appends its own, and the server is handed two conflicting
    lengths -- which h11 refuses to send, turning any page large enough to
    compress into a 502 while smaller ones on the same site are fine.

    Normalising here rather than reaching into the compressor keeps the fix
    where the contract is broken, so anything else mounted over Django is
    covered by it too.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def lowercased(message):
            if message["type"] == "http.response.start":
                message = {
                    **message,
                    "headers": [
                        (name.lower(), value) for name, value in message["headers"]
                    ],
                }
            await send(message)

        await self.app(scope, receive, lowercased)

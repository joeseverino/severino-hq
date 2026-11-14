"""The development servers' Uvicorn, listening on both loopback stacks when its host is loopback.

A browser asks `localhost` for ::1 as readily as for 127.0.0.1, and Uvicorn's
command line binds one host (Kludex/uvicorn#1528; #2486, which would have
taken several, closed unmerged). So this binds 127.0.0.1 and ::1 on one port
and hands both sockets to Uvicorn's own server and reloader, as `uvicorn.run`
hands them the one it binds. A host past loopback is bound alone, as given.

The port it bound is put in `UVICORN_PORT` before any worker starts, so the
scratch server's sign-in link (hq/config/dev_asgi.py) names it.
"""

import argparse
import contextlib
import logging
import os
import socket

from uvicorn import Config, Server
from uvicorn.supervisors import ChangeReload

from hq.config.networks import is_loopback

APPLICATION = "hq.config.dev_asgi:migrated_application"
LOOPBACK = ("127.0.0.1", "::1")


def listeners(host: str, port: int) -> list[socket.socket]:
    """Sockets bound on one port: both loopback stacks for a loopback host, else ``host`` alone."""

    bound = []
    for each in LOOPBACK if is_loopback(host) else (host,):
        family = socket.AF_INET6 if ":" in each else socket.AF_INET
        sock = socket.socket(family)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind((each, port))
        # Port 0 is chosen by the first bind, and the next takes the same one.
        port = sock.getsockname()[1]
        sock.set_inheritable(True)
        bound.append(sock)
    return bound


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--reload-dir", action="append", default=[])
    parser.add_argument("--reload-exclude", action="append", default=[])
    asked = parser.parse_args()

    # No proxy headers, as the image's: the application reads its proxy's
    # forwarded headers itself, after recording the peer they replace.
    config = Config(
        APPLICATION,
        factory=True,
        proxy_headers=False,
        reload=asked.reload,
        # A template edit restarts it too: with DEBUG off, as `dev` runs,
        # templates are cached until the server restarts.
        reload_includes=["*.html"] if asked.reload else None,
        reload_dirs=asked.reload_dir or None,
        reload_excludes=asked.reload_exclude or None,
    )
    sockets = listeners(asked.host, asked.port)
    port = sockets[0].getsockname()[1]
    os.environ["UVICORN_PORT"] = str(port)
    hosts = (
        f"[{sock.getsockname()[0]}]" if sock.family == socket.AF_INET6 else sock.getsockname()[0] for sock in sockets
    )
    addresses = " and ".join(f"http://{host}:{port}" for host in hosts)
    logging.getLogger("uvicorn.error").info("Uvicorn running on %s (Press CTRL+C to quit)", addresses)

    server = Server(config)
    # Ctrl-C ends the server as uvicorn's own command does, without a traceback.
    with contextlib.suppress(KeyboardInterrupt):
        if config.should_reload:
            ChangeReload(config, target=server.run, sockets=sockets).run()
        else:
            server.run(sockets=sockets)


if __name__ == "__main__":
    main()

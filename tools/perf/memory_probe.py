"""What each module's own load holds of HQ's Python memory, at boot and after a pass over pages.

Run by tools.perf.memory as ``python -m tools.perf.memory_probe <out>``, in a process of its
own, so the count starts before HQ imports anything. tracemalloc counts the blocks Python's
allocator holds: a C library's own allocations are not in it.

A module's own share is what the memory traced grew by while its body ran, the loads it started
taken out, so a module is never charged for a heavy one it imports. Every load, a lazy import's
too when its name is first used, passes through importlib's ``_load_unlocked``, which the count
wraps. Pages are asked one at a time, so one load runs at once.

Each path named in ``MEMORY_AFTER`` is a stage of its own after the pass: the modules that serve
it, which boot leaves lazy, are loaded with every lazy import made eager, so the stage holds the
path's whole closure, the most its first call can load. With ``MEMORY_TIMED`` set nothing is
counted: the same boot, pass and paths run untraced under ``python -X importtime``, a line on
stderr naming each path before its loads, so the import times are tracemalloc's slowdown free.
"""

import _frozen_importlib as bootstrap
import os
import sys
import tracemalloc
import types

# Each loads where it is first used, once the count has started, and HQ loads it too.
lazy import asyncio
lazy import json
lazy from pathlib import Path
lazy from urllib.parse import urlsplit

lazy from django.conf import settings

lazy import hq.config.asgi as served

BOOT = "boot"
PASS = "pass"
# What a server answers once it is ready to serve, which the count of boot ends on.
READY = "/health/ready/"
REDIRECTS = {301, 302, 303, 307, 308}
# More than the longest chain of redirects within HQ a navigation entry takes.
HOPS = 5
# Each path boot never takes, by the modules that serve it: a sign-in through the OIDC provider,
# an agent's MCP call, and a call HQ makes out of the process.
AFTER = {
    "oidc": (
        "hq.platform.core.oidc",
        "hq.platform.core.oidc_views",
        "hq.platform.core.oidc_end_session",
        "hq.platform.core.oidc_logout",
        "mozilla_django_oidc.views",
    ),
    "mcp-call": ("hq.platform.mcp.server", "hq.platform.mcp.security"),
    "outbound": (
        "hq.platform.application.http_read",
        "hq.platform.application.avatars",
        "hq.platform.application.oci_registry",
        "hq.domains.control_plane.dns_lookup",
        "hq.domains.content.content_sync",
    ),
}
# What the timed run writes on stderr before a path's loads, the path's name after it.
MARK = "audit:memory path "
# The one place every load passes through: importlib has no public hook around a module's body.
_LOAD = "_load_unlocked"


class Ledger:
    """Each module's own share in bytes, and the stage it loaded in."""

    def __init__(self) -> None:
        self.own: dict[str, int] = {}
        self.stage: dict[str, str] = {}
        self.current = BOOT
        # For each load still running, what the loads it started have added.
        self._open: list[list[int]] = []
        self._original = getattr(bootstrap, _LOAD)

    def _load(self, spec):
        before = tracemalloc.get_traced_memory()[0]
        children = [0]
        self._open.append(children)
        try:
            return self._original(spec)
        finally:
            self._open.pop()
            added = tracemalloc.get_traced_memory()[0] - before
            self.own[spec.name] = self.own.get(spec.name, 0) + added - children[0]
            self.stage.setdefault(spec.name, self.current)
            if self._open:
                self._open[-1][0] += added

    def install(self) -> None:
        setattr(bootstrap, _LOAD, self._load)

    def remove(self) -> None:
        setattr(bootstrap, _LOAD, self._original)


async def ask(application, page: str, cookie: str) -> tuple[int, str]:
    """The status ``application`` answers a GET of ``page`` with, as a server asks it, and where
    it redirects to, or nothing."""

    path, _, query = page.partition("?")
    sent: list[dict] = []
    requests = [{"type": "http.request", "body": b"", "more_body": False}]

    async def receive():
        if requests:
            return requests.pop()
        # The client stays connected: Django listens for a disconnect until it has answered.
        return await asyncio.get_running_loop().create_future()

    async def send(message):
        sent.append(message)

    await application(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query.encode(),
            "root_path": "",
            "headers": [(b"host", b"127.0.0.1"), (b"cookie", cookie.encode())],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8000),
        },
        receive,
        send,
    )
    start = next(message for message in sent if message["type"] == "http.response.start")
    # A header's name keeps the case the application gave it, as Django's does.
    headers = {name.lower(): value for name, value in start.get("headers", ())}
    return start["status"], headers.get(b"location", b"").decode()


async def landing(application, page: str, cookie: str) -> dict[str, int | str]:
    """The status of the page ``page`` lands on, each redirect within HQ followed, and the
    address of a redirect left unfollowed, or nothing."""

    for _ in range(HOPS):
        answer, location = await ask(application, page, cookie)
        if answer not in REDIRECTS:
            return {"status": answer, "to": ""}
        to = urlsplit(location)
        if to.scheme or to.netloc or not to.path.startswith("/"):
            break
        page = to.path + (f"?{to.query}" if to.query else "")
    return {"status": answer, "to": location}


async def walk(application, pages: list[str], cookie: str) -> dict[str, dict[str, int | str]]:
    return {page: await landing(application, page, cookie) for page in pages}


def _eager(*_: object) -> bool:
    return False


def take(path: str) -> None:
    """Load the modules that serve ``path`` with each lazy import they reach made eager; a module
    boot already loaded has the names it left lazy resolved."""

    sys.set_lazy_imports_filter(_eager)
    for name in AFTER[path]:
        # The import statement's own path, which python -X importtime times; importlib's is not timed.
        __import__(name)
        module = sys.modules[name]
        for attribute, value in list(vars(module).items()):
            if isinstance(value, types.LazyImportType):
                getattr(module, attribute)


def timed(paths: list[str]) -> None:
    pages = os.environ["MEMORY_PAGES"].split()
    session = os.environ["MEMORY_SESSION"]
    asyncio.run(walk(served.application, [READY], ""))
    asyncio.run(walk(served.application, pages, f"{settings.SESSION_COOKIE_NAME}={session}"))
    for path in paths:
        print(f"{MARK}{path}", file=sys.stderr, flush=True)
        take(path)


def main(out: str) -> None:
    # Read before the count starts, so what reads them is not charged to HQ.
    pages = os.environ["MEMORY_PAGES"].split()
    session = os.environ["MEMORY_SESSION"]
    paths = os.environ.get("MEMORY_AFTER", "").split()
    ledger = Ledger()
    ledger.install()
    tracemalloc.start()
    ready = asyncio.run(walk(served.application, [READY], ""))
    boot = tracemalloc.get_traced_memory()[0]
    ledger.current = PASS
    landed = asyncio.run(walk(served.application, pages, f"{settings.SESSION_COOKIE_NAME}={session}"))
    after = tracemalloc.get_traced_memory()[0]
    taken = {}
    for path in paths:
        ledger.current = path
        take(path)
        taken[path] = tracemalloc.get_traced_memory()[0]
    peak = tracemalloc.get_traced_memory()[1]
    ledger.remove()
    tracemalloc.stop()
    Path(out).write_text(
        json.dumps(
            {
                BOOT: boot,
                PASS: after,
                "after": taken,
                "peak": peak,
                "ready": ready[READY],
                "pages": landed,
                "modules": {name: {"own": size, "stage": ledger.stage[name]} for name, size in ledger.own.items()},
            }
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    if os.environ.get("MEMORY_TIMED"):
        timed(os.environ.get("MEMORY_AFTER", "").split())
    else:
        main(sys.argv[1])

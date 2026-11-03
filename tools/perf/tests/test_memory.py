"""audit:memory charges each module what its own load holds, and asks pages as a server asks them."""

import asyncio
import sys
import tempfile
import tracemalloc
from pathlib import Path

from django.test import SimpleTestCase

from tools.perf import memory
from tools.perf.memory_probe import BOOT, PASS, Ledger, walk

MB = 1_000_000


class LedgerTests(SimpleTestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        sys.path.insert(0, folder.name)
        self.addCleanup(sys.path.remove, folder.name)

    def module(self, name: str, body: str) -> None:
        (self.folder / f"{name}.py").write_text(body, encoding="utf-8")
        self.addCleanup(sys.modules.pop, name, None)

    def counted(self, *names: str, stage: str = BOOT, ledger: Ledger | None = None) -> Ledger:
        ledger = ledger or Ledger()
        ledger.current = stage
        tracing = tracemalloc.is_tracing()
        ledger.install()
        if not tracing:
            tracemalloc.start()
        try:
            for name in names:
                __import__(name)
        finally:
            if not tracing:
                tracemalloc.stop()
            ledger.remove()
        return ledger

    def test_a_module_is_charged_its_own_load_and_not_the_modules_it_loads(self) -> None:
        self.module("example_memory_inner", "HELD = bytes(3_000_000)\n")
        self.module(
            "example_memory_outer",
            "import example_memory_inner\nHELD = bytes(2_000_000)\nINNER = example_memory_inner.HELD\n",
        )
        ledger = self.counted("example_memory_outer")
        self.assertAlmostEqual(ledger.own["example_memory_outer"], 2 * MB, delta=0.3 * MB)
        self.assertAlmostEqual(ledger.own["example_memory_inner"], 3 * MB, delta=0.3 * MB)

    def test_a_module_is_kept_in_the_stage_it_first_loaded_in(self) -> None:
        self.module("example_memory_early", "HELD = bytes(1_000)\n")
        self.module("example_memory_late", "HELD = bytes(1_000)\n")
        ledger = self.counted("example_memory_early")
        self.counted("example_memory_late", stage=PASS, ledger=ledger)
        self.assertEqual(ledger.stage, {"example_memory_early": BOOT, "example_memory_late": PASS})

    def test_removed_the_import_system_loads_as_it_did(self) -> None:
        self.module("example_memory_after", "HELD = 1\n")
        self.counted()
        __import__("example_memory_after")
        self.assertEqual(sys.modules["example_memory_after"].HELD, 1)


class RankingTests(SimpleTestCase):
    MODULES = {
        "example.alpha.one": {"own": 3 * MB, "stage": BOOT},
        "example.alpha.two": {"own": 2 * MB, "stage": BOOT},
        "example.beta": {"own": 4 * MB, "stage": BOOT},
        "example.alpha.three": {"own": 9 * MB, "stage": PASS},
        "hq.platform.application.ui": {"own": 1 * MB, "stage": BOOT},
        "hq.platform.core.models": {"own": 1 * MB, "stage": BOOT},
    }

    def test_a_package_sums_its_modules_of_one_stage_largest_first(self) -> None:
        self.assertEqual(
            memory.ranked(self.MODULES, BOOT),
            [("example", 9 * MB, 3), ("hq.platform.application", MB, 1), ("hq.platform.core", MB, 1)],
        )
        self.assertEqual(memory.ranked(self.MODULES, PASS), [("example", 9 * MB, 1)])

    def test_by_module_each_stands_alone(self) -> None:
        self.assertEqual(memory.ranked(self.MODULES, BOOT, by=str)[0], ("example.beta", 4 * MB, 1))

    def test_the_pass_is_counted_as_what_it_added_and_the_rest_as_outside_any_load(self) -> None:
        result = {BOOT: 12 * MB, PASS: 25 * MB, "modules": self.MODULES}
        counted = memory.totals(result)
        self.assertEqual(counted[BOOT], {"traced": 12 * MB, "modules": 5, "own": 11 * MB, "outside": MB})
        self.assertEqual(counted[PASS], {"traced": 13 * MB, "modules": 1, "own": 9 * MB, "outside": 4 * MB})


def application(routes: dict[str, tuple[int, str]], listened: list[dict]):
    """An ASGI application answering each path with its status and Location, and listening for a
    disconnect after the request, as Django's handler does."""

    async def serve(scope, receive, send):
        await receive()
        listener = asyncio.ensure_future(receive())
        status, location = routes[scope["path"]]
        await send({"type": "http.response.start", "status": status, "headers": [(b"Location", location.encode())]})
        await send({"type": "http.response.body", "body": b""})
        await asyncio.sleep(0)
        if listener.done():
            listened.append(listener.result())
        listener.cancel()

    return serve


class WalkTests(SimpleTestCase):
    def test_a_redirect_within_hq_is_followed_and_one_out_of_it_is_left(self) -> None:
        listened: list[dict] = []
        routes = {
            "/a/": (302, "/b/"),
            "/b/": (200, ""),
            "/out/": (302, "https://example.com/"),
        }
        pages = asyncio.run(walk(application(routes, listened), ["/a/", "/out/"], "sessionid=example"))
        self.assertEqual(
            pages, {"/a/": {"status": 200, "to": ""}, "/out/": {"status": 302, "to": "https://example.com/"}}
        )
        self.assertEqual(listened, [], "a second receive answered before the response was sent")

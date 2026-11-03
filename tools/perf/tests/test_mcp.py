"""audit:mcp charges what the server's load and first answers added, never the boot, the later lists
or the slow calls, names the SDK's part, and sets the slow calls at once beside the same calls one
after another on each side."""

from django.test import SimpleTestCase

from tools.perf import mcp
from tools.perf.mcp_probe import (
    ANSWERED,
    AT_ONCE,
    BOOT,
    CALLED,
    CATALOG,
    LISTED,
    LISTS,
    LOADED,
    SERIAL,
)

MB = 1_000_000
# The probe's names for the action queue's page and its item count.
QUEUE = "list_resource action_items"
QUEUE_ITEMS = f"{QUEUE} items"


class ProbeTests(SimpleTestCase):
    def test_a_package_is_charged_what_it_loaded_with_the_server(self) -> None:
        modules = {
            "example.alpha.one": {"own": 3 * MB, "stage": BOOT},
            "example.alpha.two": {"own": 2 * MB, "stage": LOADED},
            "example.beta": {"own": 4 * MB, "stage": ANSWERED},
            "example.gamma": {"own": 7 * MB, "stage": LISTED},
            "example.delta": {"own": 5 * MB, "stage": CALLED},
        }
        self.assertEqual(mcp.by_package(modules), {"example": 6 * MB})

    def test_the_sdk_share_is_its_own_modules_after_boot(self) -> None:
        modules = {
            "mcp.server": {"own": 3 * MB, "stage": BOOT},
            "mcp.server.lowlevel": {"own": 2 * MB, "stage": LOADED},
            "mcp_types._types": {"own": 4 * MB, "stage": ANSWERED},
            "example.mcp": {"own": 5 * MB, "stage": ANSWERED},
        }
        self.assertEqual(mcp.sdk_share(modules), 6 * MB)

    def test_each_side_shows_the_calls_at_once_beside_the_same_calls_one_after_another(self) -> None:
        def side(commit, serial, at_once):
            stages = dict.fromkeys((BOOT, LOADED, ANSWERED), 0)
            result = {
                "ready": 200,
                "list": {"bytes": 0, "tools": 0},
                "lists": dict.fromkeys((*LISTS, CATALOG), 0) | {QUEUE: 3000, QUEUE_ITEMS: 3},
                "tools": {},
                "traced": stages,
                "resident": stages,
                "document": {"traced": 0, "peak": 0, "resident": 0},
                "modules": {},
                "calls": {
                    SERIAL: {"resident": serial, "traced": serial, "threads": 2, "in flight": 1},
                    AT_ONCE: {"resident": at_once, "traced": at_once, "threads": 9, "in flight": 8},
                },
            }
            sets = {name: {"summary": mcp.summary(result), "result": result} for name in mcp.SETS}
            return {"commit": commit, "sets": sets}

        said = mcp.render(
            {"extensions": [], "base": side("example-base", MB, 3 * MB), "here": side("example-here", MB, 5 * MB)}
        )

        rows = {line.split("  ")[0]: line.split() for line in said.splitlines()}
        at_once = rows[f"slow calls {AT_ONCE}, resident MB while in flight"]
        self.assertEqual(at_once[-4:], ["3.00", "5.00", "3.00", "5.00"], "each set, base then here")
        self.assertEqual(rows[f"slow calls {SERIAL}, resident MB while in flight"][-1], "1.00")
        self.assertEqual(rows[f"slow calls {AT_ONCE}, calls in flight at the peak"][-1], "8")

    def test_the_queue_page_is_counted_per_item(self) -> None:
        def per_item(answer, items):
            result = {
                "list": {"bytes": 0, "tools": 0},
                "lists": dict.fromkeys((*LISTS, CATALOG), 0) | {QUEUE: answer, QUEUE_ITEMS: items},
                "traced": dict.fromkeys((BOOT, LOADED, ANSWERED), 0),
                "resident": dict.fromkeys((BOOT, LOADED, ANSWERED), 0),
                "document": {"traced": 0, "peak": 0, "resident": 0},
                "modules": {},
                "calls": {
                    phase: dict.fromkeys(("resident", "traced", "threads", "in flight"), 0)
                    for phase in (SERIAL, AT_ONCE)
                },
            }
            return mcp.summary(result)["queue per item"]

        self.assertEqual(per_item(3000, 3), 1000)
        self.assertEqual(per_item(40, 0), 40, "an empty queue is its envelope")

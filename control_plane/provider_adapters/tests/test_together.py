"""Reading several things at once keeps the sweep's context and its order."""

from __future__ import annotations

import threading
import time
from unittest import TestCase

from controller_runtime import provider_http

from ..contracts import ProviderError
from ..parts import part_ledger, refuse_part
from ..together import READ_WORKERS, read_each


class ReadEachTests(TestCase):
    def test_results_come_back_in_the_order_asked_whichever_finished_first(self):
        def read(item: int) -> int:
            # The first asked is the last to answer.
            time.sleep(0.02 * (4 - item))
            return item * 10

        self.assertEqual(read_each([1, 2, 3, 4], read), [10, 20, 30, 40])

    def test_the_waits_overlap(self):
        together = threading.Barrier(READ_WORKERS, timeout=5)

        def read(item: int) -> int:
            # Passes only if this many reads are in flight at once.
            together.wait()
            return item

        self.assertEqual(read_each(range(READ_WORKERS), read), list(range(READ_WORKERS)))

    def test_no_more_are_in_flight_than_the_limit(self):
        lock = threading.Lock()
        running, most = 0, 0

        def read(item: int) -> int:
            nonlocal running, most
            with lock:
                running += 1
                most = max(most, running)
            time.sleep(0.01)
            with lock:
                running -= 1
            return item

        read_each(range(READ_WORKERS * 3), read)

        self.assertLessEqual(most, READ_WORKERS)

    def test_readers_arriving_together_share_one_load_of_a_value(self):
        """Nothing seeded: every read wants the value at once, and one fetches it."""

        loads = []
        arrived = threading.Barrier(READ_WORKERS, timeout=5)

        def load() -> str:
            loads.append(1)
            time.sleep(0.02)
            return "value"

        def read(_item: int) -> str:
            arrived.wait()
            return provider_http.snapshot_value(("example", "shared"), load)

        with provider_http.provider_snapshot():
            self.assertEqual(read_each(range(READ_WORKERS), read), ["value"] * READ_WORKERS)

        self.assertEqual(len(loads), 1)

    def test_a_load_that_failed_is_not_kept_and_the_next_asker_tries_again(self):
        attempts = []

        def load() -> str:
            attempts.append(1)
            if len(attempts) == 1:
                raise ProviderError("not this time")
            return "value"

        with provider_http.provider_snapshot():
            with self.assertRaises(ProviderError):
                provider_http.snapshot_value(("example", "flaky"), load)
            self.assertEqual(provider_http.snapshot_value(("example", "flaky"), load), "value")

    def test_a_failure_stops_reads_that_had_not_started(self):
        started = []

        def read(item: int) -> int:
            started.append(item)
            if item == 0:
                raise ProviderError("the first one")
            time.sleep(0.05)
            return item

        with self.assertRaises(ProviderError):
            read_each(range(READ_WORKERS * 5), read)

        self.assertLess(len(started), READ_WORKERS * 5)

    def test_a_later_item_failing_stops_the_rest_while_an_earlier_one_is_still_running(self):
        """Not only when the failed item is reached in order: the moment it fails."""

        started = []
        release = threading.Event()

        def read(item: int) -> int:
            started.append(item)
            if item == 0:
                # Slow, and first in order: the caller is waiting on this one.
                release.wait(timeout=5)
                return item
            if item == 1:
                raise ProviderError("the second one")
            time.sleep(0.01)
            return item

        def unblock():
            time.sleep(0.15)
            release.set()

        threading.Thread(target=unblock).start()
        with self.assertRaisesRegex(ProviderError, "the second one"):
            read_each(range(READ_WORKERS * 10), read)

        # Without cancelling at the failure, the other workers would have run
        # every remaining item while the first was still being waited on.
        self.assertLess(len(started), READ_WORKERS * 10)

    def test_a_part_refused_in_any_read_reaches_the_ledger_it_was_opened_under(self):
        def read(item: int) -> int:
            refuse_part("example", ProviderError("refused"), scope=f"scope-{item}")
            return item

        with part_ledger() as refused:
            read_each(range(5), read)

        self.assertEqual(sorted(entry["scope"] for entry in refused), [f"scope-{item}" for item in range(5)])

    def test_the_first_failure_in_the_order_asked_is_the_one_raised(self):
        def read(item: int) -> int:
            if item in (1, 3):
                raise ProviderError(f"item {item}")
            return item

        with self.assertRaisesRegex(ProviderError, "item 1"):
            read_each(range(5), read)

    def test_one_item_or_one_worker_is_read_in_the_callers_own_thread(self):
        here = threading.get_ident()

        self.assertEqual(read_each([1], lambda _item: threading.get_ident()), [here])
        self.assertEqual(read_each([1, 2], lambda _item: threading.get_ident(), workers=1), [here, here])

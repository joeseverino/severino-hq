"""Work outside Django's request cycle gets a thread of its own, and connections
checked before it and closed after it, except one inside an open transaction."""

import asyncio
import threading

from asgiref.sync import sync_to_async
from django.db import connection, connections
from django.test import TestCase

from ..outside_requests import connections_of_its_own, thread_of_its_own


def _query() -> object:
    """This thread's connection, after a query has opened it."""

    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return connections["default"]


class ConnectionsOfItsOwnTests(TestCase):
    def test_a_thread_closes_its_connection_when_the_work_ends(self):
        seen: list[object] = []

        def work() -> None:
            with connections_of_its_own():
                seen.append(_query())
                seen.append(seen[0].connection is not None)

        thread = threading.Thread(target=work)
        thread.start()
        thread.join()

        opened, open_inside = seen
        self.assertTrue(open_inside)
        self.assertIsNone(opened.connection)

    def test_a_connection_inside_a_transaction_is_left_to_its_opener(self):
        _query()
        with connections_of_its_own():
            _query()

        self.assertTrue(connection.in_atomic_block)
        self.assertIsNotNone(connection.connection)


class ThreadOfItsOwnTests(TestCase):
    def test_calls_in_flight_together_run_on_threads_of_their_own_and_close_after(self):
        both_in = threading.Barrier(2, timeout=10)

        def work() -> tuple[int, object]:
            opened = _query()
            # Each waits for the other, which a shared thread would never reach.
            both_in.wait()
            return threading.get_ident(), opened

        async def call() -> tuple[int, object]:
            async with thread_of_its_own():
                return await sync_to_async(work)()

        async def together():
            return await asyncio.gather(call(), call())

        (first, first_connection), (second, second_connection) = asyncio.run(together())

        self.assertEqual(len({first, second, threading.get_ident()}), 3)
        self.assertIsNone(first_connection.connection)
        self.assertIsNone(second_connection.connection)

"""Work outside Django's request cycle gets a thread of its own, and its
connections are closed after it. A connection inside an open transaction is
left to its opener, as every TestCase's transaction holds."""

import asyncio
import threading

from asgiref.sync import sync_to_async
from django.db import connection, connections
from django.test import TestCase

from ..outside_requests import thread_of_its_own


class ThreadOfItsOwnTests(TestCase):
    def test_calls_in_flight_together_run_on_threads_of_their_own_and_close_after(self):
        both_in = threading.Barrier(2, timeout=10)

        def work() -> tuple[int, object]:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
            # Each waits for the other, which a shared thread would never reach.
            both_in.wait()
            return threading.get_ident(), connections["default"]

        async def call() -> tuple[int, object]:
            async with thread_of_its_own():
                return await sync_to_async(work)()

        async def together():
            return await asyncio.gather(call(), call())

        (first, first_connection), (second, second_connection) = asyncio.run(together())

        self.assertEqual(len({first, second, threading.get_ident()}), 3)
        self.assertIsNone(first_connection.connection)
        self.assertIsNone(second_connection.connection)

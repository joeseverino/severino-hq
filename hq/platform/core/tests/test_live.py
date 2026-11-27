"""What commits, heard by the pages open on HQ through their one stream."""

from datetime import UTC, datetime
from unittest import mock

from asgiref.sync import async_to_sync
from django.db import connection
from django.test import SimpleTestCase
from django.urls import reverse

from hq.domains.jobs.models import Job
from hq.platform.core import live
from hq.platform.core.models import UpstreamReading
from hq.platform.core.testing import SignedInTestCase


def _names(event: str) -> set[str]:
    """The read sets an event of the stream names."""

    lines = dict(line.split(": ", 1) if ": " in line else (line.rstrip(":"), "") for line in event.splitlines())
    return set(lines.get("data", "").split()) if "event" not in lines else set()


def _id(event: str) -> str:
    return next(line.removeprefix("id: ") for line in event.splitlines() if line.startswith("id: "))


class WrittenTests(SimpleTestCase):
    def test_the_table_a_statement_writes_is_read_from_it(self):
        statements = {
            'INSERT INTO "core_upstreamreading" ("key") VALUES (%s)': "core_upstreamreading",
            'INSERT OR IGNORE INTO "core_upstreamreading" ("key") VALUES (%s)': "core_upstreamreading",
            'UPDATE "core_upstreamreading" SET "value" = %s': "core_upstreamreading",
            'DELETE FROM "core_upstreamreading" WHERE "id" IN (%s)': "core_upstreamreading",
            '  update "core_upstreamreading" set value = 1': "core_upstreamreading",
        }
        for sql, table in statements.items():
            with self.subTest(sql=sql):
                self.assertEqual(live.WRITTEN.match(sql)[1], table)
        for sql in ('SELECT "key" FROM "core_upstreamreading"', "SAVEPOINT s1", "PRAGMA foreign_keys"):
            with self.subTest(sql=sql):
                self.assertIsNone(live.WRITTEN.match(sql))

    def test_a_wrapper_of_one_block_leaves_the_watch_in_place(self):
        def passing(execute, sql, params, many, context):
            return execute(sql, params, many, context)

        with live.heard(lambda _table: None):
            connection.execute_wrappers.remove(live.noted)
            with connection.execute_wrapper(passing):
                # As when a connection opens inside the block.
                live._watch(connection)

            self.assertEqual(connection.execute_wrappers, [live.noted])

    def test_nothing_is_watched_once_nothing_listens(self):
        with live.heard(lambda _table: None), live.heard(lambda _table: None):
            self.assertIn(live.noted, connection.execute_wrappers)

        self.assertNotIn(live.noted, connection.execute_wrappers)


class StreamTests(SignedInTestCase):
    """A page's stream: each read set a commit changed, from the change it last heard."""

    def setUp(self):
        super().setUp()
        listening = live.listening()
        listening.__enter__()
        self.addCleanup(listening.__exit__, None, None, None)

    def _committed(self, write):
        with self.captureOnCommitCallbacks(execute=True):
            write()

    def _events(self, *, after="", heard=""):
        """The stream's first change or beat, as a page that last heard
        ``heard`` (``Last-Event-ID``), or that was drawn after ``after``, opens it."""

        async def opened():
            await self.async_client.aforce_login(self.user)
            headers = {"Last-Event-ID": heard} if heard else {}
            response = await self.async_client.get(reverse("live"), {"after": after} if after else {}, headers=headers)
            try:
                said = []
                async for chunk in response.streaming_content:
                    said.append(chunk.decode())
                    if said[-1].startswith(("id:", "event:")):
                        return response, said[-1]
            finally:
                response.close()

        return async_to_sync(opened)()

    def test_a_page_hears_each_read_set_a_commit_changed_and_only_what_came_after(self):
        drawn = live.now()
        self._committed(lambda: UpstreamReading.objects.create(key="example", value={}, observed_at=datetime.now(UTC)))

        response, event = self._events(after=drawn)

        self.assertEqual(response["Content-Type"], "text/event-stream")
        self.assertEqual(response["X-Accel-Buffering"], "no")
        self.assertIn("estate", _names(event))
        self.assertNotIn("run", _names(event))
        _response, again = self._events(heard=_id(event))
        self.assertEqual(_names(again), set())

        self._committed(lambda: Job.objects.create(kind="example.work", label="Example"))
        _response, later = self._events(heard=_id(event))
        self.assertEqual(_names(later), {"run"})

    def test_a_change_this_process_cannot_answer_for_names_every_read_set(self):
        _response, event = self._events(heard="elsewhere.5")

        self.assertEqual(_names(event), set(live.reads()))

    def test_a_speculative_request_is_refused_the_stream(self):
        response = self.client.get(reverse("live"), headers={"Sec-Purpose": "prefetch"})

        self.assertEqual(response.status_code, 503)

    def test_a_person_holds_only_so_many_streams(self):
        async def opening():
            await self.async_client.aforce_login(self.user)
            first = await self.async_client.get(reverse("live"))
            refused = await self.async_client.get(reverse("live"))
            first.close()
            after_one_closed = await self.async_client.get(reverse("live"))
            after_one_closed.close()
            return first.status_code, refused.status_code, after_one_closed.status_code

        # A hub of its own: a page walk elsewhere opens streams it never closes.
        with mock.patch.object(live, "STREAMS_PER_PERSON", 1), mock.patch.object(live, "HUB", live._Hub()):
            self.assertEqual(async_to_sync(opening)(), (200, 429, 200))

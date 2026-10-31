"""The MCP application, started by the first request that needs it and stopped with the lifespan."""

import asyncio
import contextlib

from django.test import SimpleTestCase

from hq.config.asgi import StartedOnDemand


class Recorder:
    """A served application that counts its starts and stops and answers every request."""

    def __init__(self, *, failures: int = 0):
        self.starts = 0
        self.stops = 0
        self.answered = 0
        self.failures = failures

    @contextlib.asynccontextmanager
    async def serving(self):
        self.starts += 1
        if self.failures:
            self.failures -= 1
            raise OSError("cannot start")
        try:
            yield self.application
        finally:
            self.stops += 1

    async def application(self, scope, receive, send):
        await asyncio.sleep(0)
        self.answered += 1


def request(on_demand):
    return on_demand({"type": "http"}, None, None)


class StartedOnDemandTests(SimpleTestCase):
    def test_nothing_starts_until_a_request_and_then_once(self):
        recorder = Recorder()
        on_demand = StartedOnDemand(recorder.serving)

        async def lifespan():
            async with on_demand.running():
                await asyncio.sleep(0)
                started_idle = recorder.starts
                await asyncio.gather(*(request(on_demand) for _ in range(5)))
                await request(on_demand)
                return started_idle

        self.assertEqual(asyncio.run(lifespan()), 0)
        self.assertEqual((recorder.starts, recorder.answered, recorder.stops), (1, 6, 1))

    def test_a_lifespan_that_took_no_request_starts_nothing(self):
        recorder = Recorder()

        async def lifespan():
            async with StartedOnDemand(recorder.serving).running():
                pass

        asyncio.run(lifespan())
        self.assertEqual((recorder.starts, recorder.stops), (0, 0))

    def test_a_request_outside_the_lifespan_is_refused(self):
        recorder = Recorder()
        with self.assertRaises(RuntimeError):
            asyncio.run(request(StartedOnDemand(recorder.serving)))
        self.assertEqual(recorder.starts, 0)

    def test_a_failed_start_fails_its_request_and_the_next_starts_again(self):
        recorder = Recorder(failures=1)
        on_demand = StartedOnDemand(recorder.serving)

        async def lifespan():
            async with on_demand.running():
                with self.assertRaises(OSError):
                    await request(on_demand)
                await request(on_demand)

        asyncio.run(lifespan())
        self.assertEqual((recorder.starts, recorder.answered, recorder.stops), (2, 1, 1))

    def test_a_new_lifespan_starts_it_again(self):
        recorder = Recorder()
        on_demand = StartedOnDemand(recorder.serving)

        async def lifespan():
            async with on_demand.running():
                await request(on_demand)

        asyncio.run(lifespan())
        asyncio.run(lifespan())
        self.assertEqual((recorder.starts, recorder.answered, recorder.stops), (2, 2, 2))

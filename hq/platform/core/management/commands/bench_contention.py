"""Browse while the database is being written, and say what that costs.

    DJANGO_STATIC_ROOT=/tmp/hq-static python manage.py bench_contention

Seeds Django's test database (never the real one) as ``bench_pages`` does,
then browses every page from several threads twice: alone, and while writers
run beside it. The writers are the two that hold SQLite's one write lock in
production: the controller landing a report (``bench.unchanged_sweep``, the
bridge's own actions) every ``--sweep-every`` seconds, and an import writing
rows in batches back to back, one transaction each, as a job the runner runs.

The web application's pass runs too (``core.ahead``): what a write unsettles
is asked again off any request, so a page finds it stored. ``--no-ahead``
leaves it out, so every page derives what a write unsettled.

Every statement each thread runs is timed. A ``BEGIN IMMEDIATE`` is where a
connection asks for the write lock, and so is a write outside a transaction,
so their time is the wait for it, read against the busy timeout that turns a
long wait into an error. Reported per
phase: page latency (median, p95, p99, worst), the writes and lock waits a
browsing request makes, what the writers waited and took, every error, the
derivations requests ran, and the process's peak memory.
"""

import random
import re
import resource
import statistics
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any, override

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import OperationalError, connection, transaction
from django.test import Client
from django.test.utils import setup_databases, teardown_databases

from hq.platform.application.derivations import counting
from hq.platform.core.ahead import keeping
from hq.platform.core.bench import Seeded, seed, unchanged_sweep
lazy from hq.domains.expenses.models import Expense
lazy from hq.domains.jobs import runner

from .bench_pages import _held_outside, _machine_client, _pages

# A statement that writes, and the table it writes to.
WRITE = re.compile(r'^\s*(INSERT(?: OR \w+)? INTO|UPDATE|DELETE FROM|REPLACE INTO)\s+"?(\w+)', re.IGNORECASE)


@dataclass
class Tally:
    """What one role saw in one phase, gathered from all its threads."""

    times: list[float] = field(default_factory=list)
    waits: list[float] = field(default_factory=list)
    writes: Counter[str] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)
    # The slowest, with what each was, longest first.
    slowest: list[tuple[float, str]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, took: float, waits: list[float], writes: Counter[str], error: str = "", what: str = "") -> None:
        with self.lock:
            self.times.append(took)
            self.slowest = sorted([*self.slowest, (took, what)], reverse=True)[:5]
            self.waits.extend(waits)
            self.writes.update(writes)
            if error:
                self.errors[error] += 1


@contextmanager
def _statements() -> Iterator[dict[str, Any]]:
    """Time this thread's statements: the lock waits, and how many write."""

    seen: dict[str, Any] = {"waits": [], "writes": Counter()}

    def timed(execute, sql, params, many, context):
        started = time.perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            took = (time.perf_counter() - started) * 1000
            if sql.lstrip()[:5].upper() == "BEGIN":
                seen["waits"].append(took)
            elif written := WRITE.match(sql):
                seen["writes"][f"{written[1].split()[0].upper()} {written[2]}"] += 1
                if not connection.in_atomic_block:
                    # Outside a transaction the statement takes the lock itself.
                    seen["waits"].append(took)

    with connection.execute_wrapper(timed):
        yield seen


def _once(tally: Tally, work: Callable[[], None], what: str = "") -> None:
    """Run ``work`` once on this thread and add what it cost to ``tally``."""

    with _statements() as seen:
        started = time.perf_counter()
        error = ""
        try:
            work()
        except OperationalError as exc:
            error = f"OperationalError: {exc}"
        except Exception as exc:  # noqa: BLE001 - a bench reports every failure and keeps going
            error = type(exc).__name__
        tally.add((time.perf_counter() - started) * 1000, seen["waits"], seen["writes"], error, what)


def _loop(go: threading.Event, stop: threading.Event, step: Callable[[], None]) -> None:
    go.wait()
    try:
        while not stop.is_set():
            step()
    finally:
        connection.close()


def _reader(seeded: Seeded, urls: list[str], tally: Tally, headers: dict[str, str], seed_value: int):
    client = Client(headers=headers, raise_request_exception=False)
    client.force_login(seeded.user)
    order = random.Random(seed_value)
    pending: list[str] = []

    def step() -> None:
        if not pending:
            pending.extend(order.sample(urls, len(urls)))
        url = pending.pop()

        def get() -> None:
            response = client.get(url)
            response.getvalue()
            if response.status_code >= 500:
                raise RuntimeError(f"{response.status_code}")

        _once(tally, get, url)

    return step


def _controller(tally: Tally, every: float, stop: threading.Event):
    """A report every ``every`` seconds; none at all is back to back."""

    def step() -> None:
        _once(tally, unchanged_sweep)
        stop.wait(every)

    return step


def _importer(seeded: Seeded, tally: Tally, batch: int, stop: threading.Event):
    """An import job into a table pages read, run as the runner runs one.

    ``batch`` rows in one transaction, then the next transaction takes them out
    again, until the phase ends: every batch unsettles what reads the table,
    and the estate stays the size it was seeded at.
    """

    template = Expense.objects.get(pk=seeded.expense.pk)
    fields = {f.attname: getattr(template, f.attname) for f in Expense._meta.concrete_fields if not f.primary_key}

    def batches(progress) -> dict[str, Any]:
        written: list[int] = []

        def write() -> None:
            with transaction.atomic():
                if written:
                    Expense.objects.filter(pk__in=written).delete()
                    written.clear()
                else:
                    rows = Expense.objects.bulk_create([Expense(**fields) for _ in range(batch)], batch_size=batch)
                    written.extend(row.pk for row in rows)

        while not stop.is_set():
            _once(tally, write)
        return {}

    return lambda: runner.run("example.bench.import", "Bench import", batches)


def _percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, round(len(ordered) * share) - 1)] if ordered else 0.0


def _peak_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1 << 20) if sys.platform == "darwin" else peak / 1024


class Command(BaseCommand):
    help = "Browse every page from several threads, alone and while writers run, and report the contention."

    @override
    def add_arguments(self, parser):
        parser.add_argument("--scale", type=float, default=1.0, help="Multiplies the seeded counts.")
        parser.add_argument("--readers", type=int, default=4, help="Browsing threads.")
        parser.add_argument("--seconds", type=float, default=20.0, help="How long each phase browses.")
        parser.add_argument("--batch", type=int, default=500, help="Rows an import writes per transaction.")
        parser.add_argument(
            "--sweep-every",
            type=float,
            default=5.0,
            help="Seconds between controller reports (production: about 60; 0 is back to back).",
        )
        parser.add_argument("--only", default="", help="Browse only pages whose name contains this.")
        parser.add_argument(
            "--no-ahead",
            action="store_true",
            help="Do not ask again what a write unsettled, as the web application does; requests derive it.",
        )

    @override
    def handle(self, *args, **options):
        if not settings.STATIC_LIVE:
            call_command("collectstatic", interactive=False, verbosity=0)
        databases = setup_databases(verbosity=0, interactive=False)
        try:
            seeded = seed(options["scale"])
            urls = [url for page, url in _pages(seeded)[0] if options["only"] in page]
            self.stdout.write(f"{len(urls)} pages, {options['readers']} readers, {options['seconds']:.0f}s a phase")
            with _held_outside():
                # The first sweep of a seeded estate adopts what it finds.
                unchanged_sweep()
            for writers in (False, True):
                self._phase(seeded, urls, options, writers)
        finally:
            teardown_databases(databases, verbosity=0)

    def _phase(self, seeded: Seeded, urls: list[str], options: dict[str, Any], writers: bool) -> None:
        reading, sweeping, importing = Tally(), Tally(), Tally()
        go, stop = threading.Event(), threading.Event()
        with _machine_client() as headers:
            steps = [_reader(seeded, urls, reading, headers, index) for index in range(options["readers"])]
            if writers:
                steps += [
                    _controller(sweeping, options["sweep_every"], stop),
                    _importer(seeded, importing, options["batch"], stop),
                ]
            # Made before anything is held: holding a job replaces threading.Thread.
            threads = [threading.Thread(target=_loop, args=(go, stop, step)) for step in steps]
            for thread in threads:
                thread.start()
            # The web application's pass, started before anything is held for
            # the same reason, and before any thread has a connection to watch.
            ahead = nullcontext() if options["no_ahead"] else keeping()
            with ahead, _held_outside(), counting() as (ran, served):
                go.set()
                time.sleep(options["seconds"])
                stop.set()
                for thread in threads:
                    thread.join()
        self._report("browsing with writers" if writers else "browsing alone", reading, sweeping, importing)
        requests = max(len(reading.times), 1)
        self.stdout.write(
            f"  derivations, the pass's included: {sum(ran.values())} ran ({sum(ran.values()) / requests:.2f} a page), "
            f"{sum(served.values())} served; peak memory {_peak_mb():.0f} MB"
        )
        for name, times in ran.most_common(5):
            self.stdout.write(f"    {times:5d}x {name}")

    def _report(self, phase: str, reading: Tally, sweeping: Tally, importing: Tally) -> None:
        timeout = settings.DATABASES["default"]["OPTIONS"].get("timeout", 5) * 1000
        self.stdout.write(f"\n{phase}")
        for role, tally in (("page", reading), ("controller sweep", sweeping), ("import batch", importing)):
            if not tally.times:
                continue
            count = len(tally.times)
            waits = tally.waits or [0.0]
            self.stdout.write(
                f"  {role:17s} {count:6d} done  median {statistics.median(tally.times):8.1f}ms  "
                f"p95 {_percentile(tally.times, 0.95):8.1f}ms  p99 {_percentile(tally.times, 0.99):8.1f}ms  "
                f"worst {max(tally.times):8.1f}ms"
            )
            self.stdout.write(
                f"  {'':17s} writes {tally.writes.total() / count:6.2f} each; lock asked {len(tally.waits) / count:5.2f} "
                f"each, waited p95 {_percentile(waits, 0.95):7.1f}ms worst {max(waits):7.1f}ms "
                f"({max(waits) / timeout:.1%} of the {timeout / 1000:.0f}s busy timeout)"
            )
            for took, what in tally.slowest if role == "page" else ():
                self.stdout.write(f"  {'':17s} {took:8.1f}ms {what}")
            for written, times in tally.writes.most_common(4):
                self.stdout.write(f"  {'':17s} {times / count:6.2f} {written}")
            for error, times in tally.errors.most_common():
                self.stdout.write(f"  {'':17s} {times}x {error}")

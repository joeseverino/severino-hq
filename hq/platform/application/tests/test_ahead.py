"""Derived answers asked again as their inputs change, ahead of the next request."""

import threading
import time
from datetime import UTC, datetime, timedelta
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.db import connection, transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.utils import timezone

from hq.domains.expenses.models import Expense
from hq.platform.application import derivations
from hq.platform.application.cadence import recently_used
from hq.platform.application.demo import demo_scope, showing_demo
from hq.platform.application.derivations import counting, derive_ahead
from hq.platform.application.projection import projection_scope, read_once, seeded
from hq.platform.core import ahead
from hq.platform.core.models import UpstreamReading

from .test_derivations import declared

READING = "core.UpstreamReading"
MOMENT = datetime(2026, 1, 1, 18, 0, tzinfo=UTC)


def _readings() -> int:
    return UpstreamReading.objects.count()


def _write(key: str = "one") -> None:
    UpstreamReading.objects.create(key=key, value={}, observed_at=MOMENT)


class Remembering(TestCase):
    def setUp(self):
        derivations.forget()
        self.addCleanup(derivations.forget)


class AskedAgainTests(Remembering):
    def test_a_question_a_request_asked_is_derived_when_its_table_is_written(self):
        with declared("test.ahead.count", reads=(READING,)) as (register, calls):
            count = register(_readings)
            with projection_scope():
                self.assertEqual(count(), 0)
            _write()

            self.assertEqual(derive_ahead(), ["test.ahead.count"])
            with projection_scope(), counting() as (ran, served):
                self.assertEqual(count(), 1)

        self.assertEqual((dict(ran), served["test.ahead.count"]), ({}, 1))
        self.assertEqual(len(calls), 2)

    def test_an_answer_that_still_stands_is_not_asked_again(self):
        with declared("test.ahead.standing", reads=(READING,)) as (register, calls):
            count = register(_readings)
            with projection_scope():
                count()

            self.assertEqual(derive_ahead(), [])
            self.assertEqual(derive_ahead(), [])

        self.assertEqual(len(calls), 1)

    def test_it_is_derived_once_however_many_writes_landed(self):
        with declared("test.ahead.once", reads=(READING,)) as (register, calls):
            count = register(_readings)
            with projection_scope():
                count()
            for key in ("a", "b", "c"):
                _write(key)

            derive_ahead()
            derive_ahead()

        self.assertEqual(len(calls), 2)

    def test_it_is_asked_as_it_was_first_asked(self):
        def reading(suffix):
            return (showing_demo(), read_once("example.seed", lambda: "unseeded"), suffix, _readings())

        with declared("test.ahead.context", reads=(READING,), vary=lambda suffix: (suffix, showing_demo())) as (
            register,
            _calls,
        ):
            read = register(reading)
            with demo_scope(True), projection_scope({"example.seed": "seeded"}):
                self.assertEqual(seeded(), {"example.seed": "seeded"})
                read("x")
            _write()

            derive_ahead()
            with demo_scope(True), projection_scope(), counting() as (ran, _served):
                found = read("x")

        self.assertEqual(found, (True, "seeded", "x", 1))
        self.assertEqual(dict(ran), {})

    def test_a_question_asked_ahead_reads_nothing_the_request_read(self):
        with declared("test.ahead.apart", reads=(READING,)) as (register, _calls):
            read = register(lambda: read_once("example.memo", _readings))
            with projection_scope():
                read()
                _write()
                # The request's own projection keeps what it read.
                self.assertEqual(read_once("example.memo", _readings), 0)

            derive_ahead()
            with projection_scope():
                self.assertEqual(read(), 1)

    def test_a_derivation_asked_by_another_is_not_remembered_on_its_own(self):
        with (
            declared("test.ahead.inner", reads=(READING,)) as (register_inner, _),
            declared("test.ahead.outer", reads=(READING,)) as (register_outer, _calls),
        ):
            inner = register_inner(_readings)
            outer = register_outer(lambda: inner() + 1)
            with projection_scope():
                outer()

            self.assertEqual(set(derivations._ASKS), {"test.ahead.outer"})

    def test_only_the_ways_last_asked_are_kept(self):
        with declared("test.ahead.kept", reads=(READING,), ahead=2) as (register, calls):
            read = register(lambda window: (window, _readings()))
            for window in ("january", "february", "march"):
                with projection_scope():
                    read(window)
            _write()

            derive_ahead()

        self.assertEqual(list(calls[3:]), [("february",), ("march",)])

    def test_a_derivation_that_keeps_none_is_never_asked_ahead(self):
        with declared("test.ahead.none", reads=(READING,), ahead=0) as (register, calls):
            read = register(_readings)
            with projection_scope():
                read()
            _write()

            self.assertEqual(derive_ahead(), [])

        self.assertEqual(len(calls), 1)

    def test_a_question_nobody_asked_lately_is_forgotten(self):
        with declared("test.ahead.old", reads=(READING,)) as (register, _calls):
            read = register(_readings)
            with projection_scope():
                read()
            _write()
            later = time.monotonic() + derivations.ASKED_WITHIN + 1

            with mock.patch.object(derivations.monotonic_time, "monotonic", return_value=later):
                self.assertEqual(derive_ahead(), [])

        self.assertEqual(derivations._ASKS["test.ahead.old"], {})

    def test_one_that_fails_is_forgotten_and_the_rest_are_asked(self):
        state = {"fail": False}

        def failing():
            if state["fail"]:
                raise RuntimeError("no")
            return _readings()

        with (
            declared("test.ahead.fails", reads=(READING,)) as (register_failing, _),
            declared("test.ahead.works", reads=(READING,)) as (register, _calls),
        ):
            fails, works = register_failing(failing), register(_readings)
            with projection_scope():
                fails()
                works()
            _write()
            state["fail"] = True

            with self.assertLogs("severino.derivations", level="ERROR"):
                asked = derive_ahead()

            self.assertEqual(asked, ["test.ahead.works"])
            self.assertEqual(derivations._ASKS["test.ahead.fails"], {})

    def test_an_answer_that_cannot_be_kept_is_not_derived_in_a_loop(self):
        with declared("test.ahead.unkept", reads=(READING,)) as (register, calls):
            read = register(lambda: (*(lambda: None, _readings())[1:], lambda: None))
            with projection_scope(), self.assertLogs("severino.derivations", level="ERROR"):
                read()
            _write()

            with self.assertLogs("severino.derivations", level="ERROR"):
                derive_ahead()
            self.assertEqual(derive_ahead(), [])

        self.assertEqual(len(calls), 2)

    def test_a_measurement_with_the_store_bypassed_is_not_remembered(self):
        with declared("test.ahead.uncached", reads=(READING,)) as (register, _calls):
            read = register(_readings)
            with projection_scope(), derivations.uncached():
                read()

        self.assertNotIn("test.ahead.uncached", derivations._ASKS)

    def test_it_wakes_next_when_the_earliest_answer_stops_standing(self):
        soon, later = MOMENT + timedelta(minutes=5), MOMENT + timedelta(hours=2)

        with (
            mock.patch.object(derivations, "_clock", return_value=MOMENT),
            declared("test.ahead.soon", reads=(READING,)) as (register_soon, _),
            declared("test.ahead.later", reads=(READING,)) as (register_later, _calls),
        ):
            first = register_soon(lambda: derivations.reached(soon))
            second = register_later(lambda: derivations.reached(later))
            self.assertIsNone(derivations.next_due())
            with projection_scope():
                first()
                second()

            self.assertEqual(derivations.next_due(), soon)

    def test_using_hq_is_what_a_question_asked_ahead_is_on_behalf_of(self):
        seen = []

        def use():
            seen.append((derivations.asked_ahead(), recently_used()))
            return _readings()

        with (
            mock.patch("hq.platform.application.cadence._path") as path,
            declared("test.ahead.use", reads=(READING,)) as (register, _calls),
        ):
            path.return_value.stat.side_effect = OSError
            read = register(use)
            with projection_scope():
                read()
            _write()
            derive_ahead()

        self.assertEqual(seen, [(False, False), (True, True)])


class UnseenTests(Remembering):
    """An answer whose computation may read the clock unseen stands a minute at most."""

    def _until(self, now: datetime, **options):
        with (
            mock.patch.object(derivations, "_clock", return_value=now),
            declared("test.unseen", reads=(READING,), **options) as (register, _calls),
        ):
            read = register(_readings)
            with projection_scope():
                read()
                return derivations.standing(read).until

    def test_it_stands_a_minute(self):
        self.assertEqual(self._until(MOMENT, unseen=True), MOMENT + derivations.UNSEEN)

    def test_it_never_stands_past_local_midnight(self):
        midnight = timezone.make_aware(
            datetime.combine(timezone.localtime(MOMENT).date() + timedelta(days=1), datetime.min.time())
        )

        self.assertEqual(self._until(midnight - timedelta(seconds=20), unseen=True), midnight)

    def test_a_derivation_the_host_follows_to_the_clock_names_no_moment(self):
        self.assertIsNone(self._until(MOMENT))

    def test_what_asks_it_stands_no_longer_than_it_does(self):
        with (
            mock.patch.object(derivations, "_clock", return_value=MOMENT),
            declared("test.unseen.part", reads=(READING,), unseen=True) as (register_part, _),
            declared("test.unseen.whole", reads=(READING,)) as (register, _calls),
        ):
            part = register_part(_readings)
            whole = register(lambda: part() + 1)
            with projection_scope():
                whole()
                until = derivations.standing(whole).until

        self.assertEqual(until, MOMENT + derivations.UNSEEN)


class AskedTwiceTests(Remembering):
    """What one derivation is told by another holds whether or not the other
    was already answered in this projection."""

    def test_an_answer_already_given_still_bounds_the_one_that_asks_next(self):
        with (
            mock.patch.object(derivations, "_clock", return_value=MOMENT),
            declared("test.twice.part", reads=(READING,), unseen=True) as (register_part, _),
            declared("test.twice.whole", reads=(READING,)) as (register, _calls),
        ):
            part = register_part(_readings)
            whole = register(lambda: part() + 1)
            with projection_scope():
                part()
                whole()
                until = derivations.standing(whole).until

        self.assertEqual(until, MOMENT + derivations.UNSEEN)

    def test_an_answer_already_given_still_says_what_it_reads(self):
        with (
            declared("test.twice.reads", reads=(), unseen=True) as (register_part, _),
            declared("test.twice.asks", reads=(), unseen=True) as (register, calls),
        ):
            part = register_part(_readings)
            whole = register(lambda: part() + 1)
            with projection_scope():
                part()
                self.assertEqual(whole(), 1)
            _write()

            with projection_scope():
                self.assertEqual(whole(), 2)

        self.assertEqual(len(calls), 2)

    def test_what_the_request_already_read_is_read_again_where_it_is_seen(self):
        """An unseen computation learns its tables from the statements it runs,
        so a read the request made before it does not hide one."""

        with declared("test.twice.memo", reads=(), unseen=True) as (register, calls):
            read = register(lambda: read_once("example.shared", _readings))
            with projection_scope():
                read_once("example.shared", _readings)
                self.assertEqual(read(), 0)
            _write()

            with projection_scope():
                self.assertEqual(read(), 1)

        self.assertEqual(len(calls), 2)

    def test_a_derivation_that_declares_its_reads_is_refused_one_it_did_not_declare(self):
        with (
            declared("test.twice.declared", reads=(READING,)) as (register_part, _),
            declared("test.twice.undeclared", reads=()) as (register, _calls),
        ):
            part = register_part(_readings)
            whole = register(lambda: part())
            with projection_scope():
                part()
                with self.assertRaises(ImproperlyConfigured):
                    whole()

    def test_one_whose_reads_are_learned_may_ask_any(self):
        with (
            declared("test.twice.any", reads=(READING,)) as (register_part, _),
            declared("test.twice.learns", reads=(), unseen=True) as (register, _calls),
        ):
            part = register_part(_readings)
            whole = register(lambda: part())
            with projection_scope():
                self.assertEqual(whole(), 0)

            self.assertIn(UpstreamReading._meta.db_table, whole.derivation.tables)


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
                self.assertEqual(ahead._WRITTEN.match(sql)[1], table)
        for sql in ('SELECT "key" FROM "core_upstreamreading"', "SAVEPOINT s1", "PRAGMA foreign_keys"):
            with self.subTest(sql=sql):
                self.assertIsNone(ahead._WRITTEN.match(sql))


class KeptCurrentTests(Remembering):
    """A committed write to a table a remembered question reads derives it again."""

    def _asked(self, register):
        read = register(_readings)
        with projection_scope():
            read()
        return read

    def test_a_committed_write_derives_ahead_and_is_then_served(self):
        with declared("test.kept.write", reads=(READING,)) as (register, calls), ahead.keeping(inline=True):
            read = self._asked(register)

            with self.captureOnCommitCallbacks(execute=True):
                _write()

            self.assertEqual(len(calls), 2)
            with projection_scope(), counting() as (ran, _served):
                self.assertEqual(read(), 1)
            self.assertEqual(dict(ran), {})

    def test_a_write_to_a_table_no_question_reads_wakes_nothing(self):
        with declared("test.kept.other", reads=(READING,)) as (register, _calls), ahead.keeping(inline=True):
            self._asked(register)

            with self.captureOnCommitCallbacks() as callbacks:
                Expense.objects.all().delete()
                connection.cursor().execute('DELETE FROM "expenses_expense"')

        self.assertEqual(callbacks, [])

    def test_a_write_that_is_rolled_back_derives_nothing(self):
        with declared("test.kept.rolled", reads=(READING,)) as (register, calls), ahead.keeping(inline=True):
            self._asked(register)

            with self.captureOnCommitCallbacks(execute=True):
                try:
                    with transaction.atomic():
                        _write()
                        raise RuntimeError("undone")
                except RuntimeError:
                    pass

        self.assertEqual(len(calls), 1)

    def test_a_wrapper_of_one_block_leaves_the_watch_in_place(self):
        def passing(execute, sql, params, many, context):
            return execute(sql, params, many, context)

        with ahead.keeping(inline=True):
            connection.execute_wrappers.remove(ahead.note_write)
            with connection.execute_wrapper(passing):
                # As when a connection opens inside the block.
                ahead._watch(connection)

            self.assertEqual(connection.execute_wrappers, [ahead.note_write])

    def test_nothing_is_watched_once_keeping_ends(self):
        with ahead.keeping(inline=True):
            self.assertIn(ahead.note_write, connection.execute_wrappers)

        self.assertNotIn(ahead.note_write, connection.execute_wrappers)
        self.assertFalse(ahead.KEEPER.inline)


class KeeperThreadTests(TransactionTestCase):
    """The thread the web application runs: woken by a commit, stopped with it."""

    def setUp(self):
        derivations.forget()
        self.addCleanup(derivations.forget)

    def test_a_commit_wakes_the_thread_and_the_answer_is_stored_off_the_request(self):
        with declared("test.keeper.thread", reads=(READING,)) as (register, calls):
            read = register(_readings)
            with projection_scope():
                read()
            with ahead.keeping():
                _write()
                deadline = time.monotonic() + 10
                while len(calls) < 2 and time.monotonic() < deadline:
                    time.sleep(0.02)
                thread = ahead.KEEPER._thread

            self.assertEqual(len(calls), 2)
            self.assertIsNone(ahead.KEEPER._thread)
            self.assertFalse(thread.is_alive())
            with projection_scope(), counting() as (ran, _served):
                self.assertEqual(read(), 1)
            self.assertEqual(dict(ran), {})

    def test_writes_that_land_together_are_one_pass(self):
        keeper = ahead._Keeper()
        with mock.patch.object(ahead, "SETTLES", 0.05):
            keeper.wake()
            threading.Timer(0.02, keeper.wake).start()
            began = time.monotonic()
            keeper._settle()
            waited = time.monotonic() - began

        self.assertGreaterEqual(waited, 0.06)
        self.assertFalse(keeper._woken.is_set())
        self.assertFalse(keeper._woken_again())
        keeper.wake()
        self.assertTrue(keeper._woken_again())
        self.assertFalse(keeper._woken_again())

    def test_a_write_during_a_pass_is_answered_before_a_waiting_request_is_let_go(self):
        passes = []

        def more():
            passes.append(derivations._NO_PASS.is_set())
            return len(passes) < 3

        derive_ahead(more)

        self.assertEqual(passes, [False, False, False])
        self.assertTrue(derivations._NO_PASS.is_set())

    def test_it_waits_for_the_earliest_moment_and_no_less_than_its_rest(self):
        with mock.patch.object(derivations, "next_due", return_value=None):
            self.assertIsNone(ahead.KEEPER._pause())
        with mock.patch.object(derivations, "next_due", return_value=timezone.now() + timedelta(minutes=5)):
            self.assertAlmostEqual(ahead.KEEPER._pause(), 300, delta=2)
        with mock.patch.object(derivations, "next_due", return_value=timezone.now() - timedelta(minutes=5)):
            self.assertEqual(ahead.KEEPER._pause(), ahead.RESTS)


class OneThreadDerivesTests(TransactionTestCase):
    """An answer two threads want at once is derived by one of them."""

    def setUp(self):
        derivations.forget()
        self.addCleanup(derivations.forget)

    def _ask_from_another_thread(self, read, found):
        def ask():
            try:
                with projection_scope():
                    found.append(read())
            finally:
                connection.close()

        thread = threading.Thread(target=ask)
        thread.start()
        return thread

    def test_the_second_waits_for_the_first_and_is_served_its_answer(self):
        started, release = threading.Event(), threading.Event()

        def slow():
            started.set()
            release.wait(10)
            return _readings()

        with declared("test.flight.slow", reads=(READING,)) as (register, calls):
            read = register(slow)
            found: list[int] = []
            first = self._ask_from_another_thread(read, found)
            self.assertTrue(started.wait(10))
            second = self._ask_from_another_thread(read, found)
            # The second is waiting on the first, not deriving beside it.
            time.sleep(0.1)
            self.assertEqual(len(calls), 1)
            release.set()
            first.join(10)
            second.join(10)

        self.assertEqual(found, [0, 0])
        self.assertEqual(len(calls), 1)

    def test_a_thread_inside_a_transaction_derives_for_itself(self):
        started, release = threading.Event(), threading.Event()
        here = threading.current_thread()

        def slow():
            if threading.current_thread() is not here:
                started.set()
                release.wait(10)
            return _readings()

        with declared("test.flight.atomic", reads=(READING,)) as (register, calls):
            read = register(slow)
            found: list[int] = []
            first = self._ask_from_another_thread(read, found)
            self.assertTrue(started.wait(10))
            # It may hold the write lock the other needs to store its answer,
            # so it does not wait on it.
            with transaction.atomic(), projection_scope():
                self.assertEqual(read(), 0)
            self.assertEqual(len(calls), 2)
            release.set()
            first.join(10)

    def test_a_derivation_that_fails_releases_whoever_waits(self):
        started, release = threading.Event(), threading.Event()
        state = {"fail": True}

        def failing():
            if state["fail"]:
                started.set()
                release.wait(10)
                raise RuntimeError("no")
            return _readings()

        with declared("test.flight.fails", reads=(READING,)) as (register, _calls):
            read = register(failing)
            found: list[int] = []

            def first_ask():
                try:
                    with projection_scope():
                        read()
                except RuntimeError:
                    # The failure is the point: what the second thread does next is asserted.
                    pass
                finally:
                    connection.close()

            first = threading.Thread(target=first_ask)
            first.start()
            self.assertTrue(started.wait(10))
            state["fail"] = False
            second = self._ask_from_another_thread(read, found)
            release.set()
            first.join(10)
            second.join(10)

        self.assertEqual(found, [0])
        self.assertEqual(derivations._IN_FLIGHT, {})

    def test_a_request_that_arrives_during_a_pass_waits_for_it_and_derives_nothing(self):
        started, release = threading.Event(), threading.Event()

        def slow():
            if derivations.asked_ahead():
                started.set()
                release.wait(10)
            return _readings()

        with (
            declared("test.flight.pass.slow", reads=(READING,)) as (register_slow, _),
            declared("test.flight.pass.other", reads=(READING,)) as (register, calls),
        ):
            first, other = register_slow(slow), register(_readings)
            with projection_scope():
                first()
                other()
            _write()

            def run_pass():
                try:
                    derive_ahead()
                finally:
                    connection.close()

            passing = threading.Thread(target=run_pass)
            passing.start()
            self.assertTrue(started.wait(10))
            found: list[int] = []
            asking = self._ask_from_another_thread(other, found)
            time.sleep(0.1)
            # The pass has not reached it yet, and the request is not deriving it.
            self.assertEqual(len(calls), 1)
            release.set()
            passing.join(10)
            asking.join(10)

        self.assertEqual(found, [1])
        self.assertEqual(len(calls), 2)
        self.assertTrue(derivations._NO_PASS.is_set())

    def test_a_pass_waits_between_questions_while_a_request_derives(self):
        started, release = threading.Event(), threading.Event()
        here = threading.current_thread()

        def slow():
            if threading.current_thread() is not here:
                started.set()
                release.wait(10)
            return _readings()

        with (
            declared("test.flight.turn.request", reads=(READING,), ahead=0) as (register_slow, _),
            declared("test.flight.turn.asked", reads=(READING,)) as (register, calls),
        ):
            request, asked = register_slow(slow), register(_readings)
            with projection_scope():
                asked()
            _write()
            found: list[int] = []
            asking = self._ask_from_another_thread(request, found)
            self.assertTrue(started.wait(10))
            self.assertFalse(derivations._NO_REQUEST.is_set())

            with mock.patch.object(derivations, "GIVES_WAY", 0.2):
                began = time.monotonic()
                derive_ahead()
                waited = time.monotonic() - began
            release.set()
            asking.join(10)

        self.assertGreaterEqual(waited, 0.2)
        self.assertEqual(len(calls), 2)
        self.assertTrue(derivations._NO_REQUEST.is_set())

    def test_a_pass_asks_first_what_a_request_is_waiting_on(self):
        started, release = threading.Event(), threading.Event()
        order: list[str] = []

        def slow():
            if derivations.asked_ahead():
                order.append("slow")
                started.set()
                release.wait(10)
            return _readings()

        def named(name):
            def read():
                if derivations.asked_ahead():
                    order.append(name)
                return _readings()

            return read

        with (
            declared("test.flight.order.slow", reads=(READING,)) as (register_slow, _),
            declared("test.flight.order.early", reads=(READING,)) as (register_early, _),
            declared("test.flight.order.wanted", reads=(READING,)) as (register_wanted, _calls),
        ):
            first = register_slow(slow)
            early = register_early(named("early"))
            wanted = register_wanted(named("wanted"))
            with projection_scope():
                first()
                early()
                wanted()
            _write()

            def run_pass():
                try:
                    derive_ahead()
                finally:
                    connection.close()

            passing = threading.Thread(target=run_pass)
            passing.start()
            self.assertTrue(started.wait(10))
            found: list[int] = []
            asking = self._ask_from_another_thread(wanted, found)
            deadline = time.monotonic() + 10
            while not derivations._WANTED["test.flight.order.wanted"] and time.monotonic() < deadline:
                time.sleep(0.01)
            release.set()
            asking.join(10)
            passing.join(10)

        self.assertEqual(order, ["slow", "wanted", "early"])
        self.assertEqual(found, [1])
        self.assertEqual(derivations._PASS_STORED, set())

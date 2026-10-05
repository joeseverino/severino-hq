"""Derived facts: computed once per change of their inputs, and never served stale."""

from __future__ import annotations

import pickle
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone as utc
from unittest import mock

from django.core.cache import caches
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, transaction
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.assets.models import Asset
from hq.domains.content.models import ContentItem
from hq.domains.control_plane.models import ManagedResource
from hq.domains.docs_index.models import DocumentationRecord
from hq.platform.application import derivations
from hq.platform.application.derivations import DERIVATIONS, counting, derivation
from hq.platform.application.expiry import days_until
from hq.platform.application.projection import projection_scope
from hq.platform.application.security import cli_principal, web_principal
from hq.platform.core import revisions
from hq.platform.core.bench import seed
from hq.platform.core.models import ActionItemRead, Revision, UpstreamReading

READING = UpstreamReading._meta.db_table
MOMENT = datetime(2026, 1, 1, 12, 0, tzinfo=utc.utc)


def _revision(table: str) -> int:
    found = revisions.read()
    assert found is not None
    return found.of([table])[0]


@contextmanager
def declared(name: str, **options):
    """A derivation that exists for one test."""

    calls: list[tuple] = []

    def register(function):
        def compute(*args):
            calls.append(args)
            return function(*args)

        return derivation(name, **options)(compute)

    try:
        yield register, calls
    finally:
        DERIVATIONS.pop(name, None)


def _readings() -> int:
    return UpstreamReading.objects.count()


class RevisionTests(TestCase):
    def test_every_model_table_carries_the_three_triggers(self):
        tables = revisions.counted_tables()
        with connection.cursor() as cursor:
            cursor.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
            present = {name for (name,) in cursor.fetchall()}

        self.assertIn(READING, tables)
        self.assertIn(ContentItem.related_documentation.through._meta.db_table, tables)
        self.assertNotIn(Revision._meta.db_table, tables)
        missing = [
            f"{table}:{event}"
            for table in tables
            for event in revisions.EVENTS
            if f"{revisions.TRIGGER_PREFIX}{table}_{event}" not in present
        ]
        self.assertEqual(missing, [])

    def test_every_way_of_writing_a_row_moves_the_revision(self):
        def raw():
            with connection.cursor() as cursor:
                cursor.execute(f'UPDATE "{READING}" SET value = %s', ['{"raw": 1}'])

        row = UpstreamReading(key="save", value={}, observed_at=MOMENT)
        writes = {
            "save": row.save,
            "create": lambda: UpstreamReading.objects.create(key="create", value={}, observed_at=MOMENT),
            "update": lambda: UpstreamReading.objects.filter(key="save").update(value={"n": 1}),
            "update_or_create": lambda: UpstreamReading.objects.update_or_create(
                key="save", defaults={"value": {"n": 2}}
            ),
            "bulk_create": lambda: UpstreamReading.objects.bulk_create(
                [UpstreamReading(key="bulk", value={}, observed_at=MOMENT)]
            ),
            "bulk_update": lambda: UpstreamReading.objects.bulk_update(
                [UpstreamReading(key="bulk", value={"n": 3}, observed_at=MOMENT)], ["value"]
            ),
            "raw": raw,
            "delete": lambda: UpstreamReading.objects.get(key="create").delete(),
            "queryset delete": lambda: UpstreamReading.objects.all().delete(),
        }
        for name, write in writes.items():
            with self.subTest(write=name):
                before = _revision(READING)
                write()
                self.assertGreater(_revision(READING), before)

    def test_a_relation_and_a_cascade_move_the_tables_they_write(self):
        seeded = seed(0.05)
        through = ContentItem.related_documentation.through._meta.db_table

        before = _revision(through)
        seeded.content.related_documentation.add(DocumentationRecord.objects.order_by("pk").last())
        self.assertGreater(_revision(through), before)

        before = _revision(through)
        ContentItem.objects.filter(pk=seeded.content.pk).delete()
        self.assertGreater(_revision(through), before)

    def test_a_write_to_one_table_leaves_another_alone(self):
        before = _revision(Asset._meta.db_table)

        UpstreamReading.objects.create(key="one", value={}, observed_at=MOMENT)

        self.assertEqual(_revision(Asset._meta.db_table), before)

    def test_a_table_without_its_triggers_has_no_revision(self):
        with connection.cursor() as cursor:
            cursor.execute(f'DROP TRIGGER "{revisions.TRIGGER_PREFIX}{READING}_update"')

        with self.assertLogs("severino.revisions", "ERROR"):
            self.assertIsNone(revisions.read().of([READING]))

        revisions.install()
        self.assertIsNotNone(revisions.read().of([READING]))

    def test_install_drops_a_trigger_for_a_table_no_model_owns(self):
        with connection.cursor() as cursor:
            cursor.execute(
                f'CREATE TRIGGER "{revisions.TRIGGER_PREFIX}gone_insert" AFTER INSERT ON '
                f'"{READING}" BEGIN SELECT 1; END'
            )

        revisions.install()

        self.assertNotIn(f"{revisions.TRIGGER_PREFIX}gone_insert", revisions.read().triggers)


class AutocommitRevisionTests(TransactionTestCase):
    def test_a_write_outside_a_transaction_moves_the_revision_with_it(self):
        before = _revision(READING)

        UpstreamReading.objects.create(key="auto", value={}, observed_at=MOMENT)

        self.assertFalse(connection.in_atomic_block)
        self.assertEqual(_revision(READING), before + 1)

    def test_a_rolled_back_write_takes_its_revision_and_its_stored_value_with_it(self):
        with declared("test.rollback", reads=("core.UpstreamReading",)) as (register, calls):
            count = register(_readings)
            self.assertEqual(count(), 0)
            before = _revision(READING)
            try:
                with transaction.atomic():
                    UpstreamReading.objects.create(key="gone", value={}, observed_at=MOMENT)
                    self.assertEqual(count(), 1)
                    raise RuntimeError
            except RuntimeError:
                pass

            self.assertEqual(_revision(READING), before)
            self.assertEqual(count(), 0)
            self.assertEqual(len(calls), 2)


class DerivationTests(TestCase):
    def test_it_runs_once_per_change_of_what_it_reads(self):
        with declared("test.count", reads=("core.UpstreamReading",)) as (register, calls):
            count = register(_readings)

            self.assertEqual((count(), count(), len(calls)), (0, 0, 1))
            Asset.objects.all().delete()
            self.assertEqual((count(), len(calls)), (0, 1))
            UpstreamReading.objects.create(key="one", value={}, observed_at=MOMENT)
            self.assertEqual((count(), count(), len(calls)), (1, 1, 2))

    def test_one_projection_asks_the_cache_once(self):
        with declared("test.scope", reads=("core.UpstreamReading",)) as (register, _calls):
            count = register(_readings)
            count()

            with projection_scope(), self.assertNumQueries(2):
                # The revisions, then the stored value; the second call asks neither.
                count()
                count()

    def test_arguments_are_answered_apart(self):
        with declared(
            "test.vary", reads=("core.UpstreamReading",), vary=lambda prefix: prefix.lower()
        ) as (register, calls):
            keys = register(lambda prefix: prefix)

            self.assertEqual((keys("a"), keys("b"), keys("A")), ("a", "b", "a"))
            self.assertEqual(len(calls), 2)

    def test_a_stored_value_holds_until_the_moment_it_named(self):
        deadline = MOMENT + timedelta(minutes=5)
        with (
            declared("test.clock", reads=("core.UpstreamReading",)) as (register, calls),
            mock.patch.object(derivations, "_clock", return_value=MOMENT) as clock,
        ):
            due = register(lambda: derivations.reached(deadline))

            self.assertEqual((due(), due(), len(calls)), (False, False, 1))
            clock.return_value = deadline
            self.assertEqual((due(), due(), len(calls)), (True, True, 2))

    def test_a_derivation_holds_no_longer_than_one_it_calls(self):
        deadline = MOMENT + timedelta(minutes=5)
        with (
            declared("test.inner", reads=("core.UpstreamReading",)) as (inner, _),
            declared("test.outer", reads=("core.UpstreamReading",)) as (outer, calls),
            mock.patch.object(derivations, "_clock", return_value=MOMENT) as clock,
        ):
            due = inner(lambda: derivations.reached(deadline))
            both = outer(lambda: (due(),))

            self.assertEqual((both(), len(calls)), ((False,), 1))
            # Answered from the cache, the inner one still passes its deadline on.
            caches[revisions.CACHE_ALIAS].delete_many(
                [key for key in self._keys() if key.startswith("test.outer")]
            )
            self.assertEqual((both(), len(calls)), ((False,), 2))
            clock.return_value = deadline
            self.assertEqual((both(), len(calls)), ((True,), 3))

    def _keys(self) -> list[str]:
        with connection.cursor() as cursor:
            cursor.execute('SELECT cache_key FROM "hq_derived"')
            return [key.split(":", 2)[2] for (key,) in cursor.fetchall()]

    def test_calling_a_derivation_that_reads_more_is_refused(self):
        with (
            declared("test.wide", reads=("core.UpstreamReading", "assets.Asset")) as (wide, _),
            declared("test.narrow", reads=("core.UpstreamReading",)) as (narrow, _calls),
        ):
            inner = wide(_readings)
            outer = narrow(lambda: inner())

            with self.assertRaisesMessage(ImproperlyConfigured, "assets_asset"):
                outer()

    def test_reading_an_undeclared_table_is_never_stored(self):
        with declared("test.undeclared", reads=("assets.Asset",)) as (register, calls):
            count = register(_readings)

            with self.assertLogs("severino.derivations", "ERROR"):
                count()
            with self.assertLogs("severino.derivations", "ERROR"):
                count()

            self.assertEqual(len(calls), 2)
            self.assertEqual(derivations.UNDECLARED.pop("test.undeclared"), {READING})

    def test_unreadable_revisions_mean_deriving(self):
        with (
            declared("test.unread", reads=("core.UpstreamReading",)) as (register, calls),
            mock.patch("hq.platform.core.revisions.read", return_value=None),
        ):
            count = register(_readings)

            self.assertEqual((count(), count(), len(calls)), (0, 0, 2))

    def test_an_unreadable_or_unwritable_cache_means_deriving(self):
        with declared("test.cache", reads=("core.UpstreamReading",)) as (register, calls):
            count = register(_readings)
            broken = mock.Mock()
            broken.get.side_effect = broken.set_many.side_effect = RuntimeError("down")

            with (
                mock.patch.object(derivations, "_store", return_value=broken),
                self.assertLogs("severino.derivations", "ERROR"),
            ):
                self.assertEqual((count(), count(), len(calls)), (0, 0, 2))

    def test_a_value_that_cannot_be_stored_is_still_the_answer(self):
        with declared("test.unpicklable", reads=("core.UpstreamReading",)) as (register, calls):
            answer = register(lambda: (lambda: 1))

            with self.assertLogs("severino.derivations", "ERROR"):
                self.assertEqual(answer()(), 1)
            self.assertEqual(len(calls), 1)

    def test_a_name_is_declared_once(self):
        with declared("test.twice", reads=("core.UpstreamReading",)) as (register, _):
            register(_readings)
            with self.assertRaises(ImproperlyConfigured):
                derivation("test.twice", reads=("core.UpstreamReading",))(_readings)

    def test_standing_names_the_revisions_and_the_moment_an_answer_holds_to(self):
        deadline = MOMENT + timedelta(minutes=5)
        with (
            declared("test.standing", reads=("core.UpstreamReading",)) as (register, _),
            mock.patch.object(derivations, "_clock", return_value=MOMENT),
        ):
            due = register(lambda: derivations.reached(deadline))

            self.assertIsNone(derivations.standing_key(_readings))
            first = derivations.standing_key(due)
            with projection_scope():
                self.assertIsNone(derivations.standing(due))
                due()
                self.assertEqual(derivations.standing(due), derivations.Standing(first, deadline))
            with projection_scope():
                due()
                self.assertEqual(derivations.standing(due).until, deadline)

            UpstreamReading.objects.create(key="one", value={}, observed_at=MOMENT)
            self.assertNotIn(derivations.standing_key(due), (None, first))

    def test_an_answer_that_was_not_stored_has_no_standing(self):
        with declared("test.unstored", reads=("assets.Asset",)) as (register, _):
            count = register(_readings)

            with projection_scope(), self.assertLogs("severino.derivations", "ERROR"):
                count()
                self.assertIsNone(derivations.standing(count))
            derivations.UNDECLARED.pop("test.unstored")


class ClockTests(SimpleTestCase):
    def _held(self, ask, now=MOMENT):
        frame = derivations._Frame(frozenset())
        token = derivations._FRAME.set(frame)
        try:
            with mock.patch.object(derivations, "_clock", return_value=now):
                return ask(), frame.until
        finally:
            derivations._FRAME.reset(token)

    def test_a_threshold_holds_until_it_is_crossed(self):
        later = MOMENT + timedelta(hours=1)

        self.assertEqual(self._held(lambda: derivations.reached(later)), (False, later))
        self.assertEqual(self._held(lambda: derivations.reached(MOMENT)), (True, None))
        self.assertEqual(self._held(lambda: derivations.passed(MOMENT)), (False, MOMENT))
        self.assertEqual(
            self._held(lambda: derivations.passed(MOMENT - timedelta(seconds=1))), (True, None)
        )

    def test_whole_units_hold_until_the_next_one(self):
        start = MOMENT - timedelta(hours=2, minutes=30)

        self.assertEqual(
            self._held(lambda: derivations.whole(start, timedelta(hours=1))),
            (2, start + timedelta(hours=3)),
        )

    def test_an_age_is_worded_to_the_minute_then_to_the_hour(self):
        recent = MOMENT - timedelta(minutes=5, seconds=20)
        old = MOMENT - timedelta(hours=7, minutes=10)

        self.assertEqual(
            self._held(lambda: derivations.since(recent)),
            (timedelta(minutes=5, seconds=20), recent + timedelta(minutes=6)),
        )
        self.assertEqual(self._held(lambda: derivations.since(old))[1], old + timedelta(hours=8))

    def test_today_holds_until_local_midnight(self):
        day, until = self._held(derivations.today)

        self.assertEqual(day, timezone.localtime(MOMENT).date())
        self.assertEqual(timezone.localtime(until).date(), day + timedelta(days=1))
        self.assertEqual(timezone.localtime(until).hour, 0)

    def test_days_left_agrees_with_itself_until_the_day_it_names_ends(self):
        for hours in (-60.2, -24.1, -1, 0.5, 11, 13, 35, 36.1, 37, 24 * 30 + 3):
            when = MOMENT + timedelta(hours=hours)
            days, until = self._held(lambda: days_until(when))
            with self.subTest(hours=hours):
                self.assertGreater(until, MOMENT)
                just_before = until - timedelta(seconds=1)
                self.assertEqual(self._held(lambda: days_until(when), just_before)[0], days)
                just_after = until + timedelta(seconds=1)
                self.assertNotEqual(
                    self._held(lambda: days_until(when), just_after)[0], days
                )

    def test_outside_a_derivation_the_clock_only_answers(self):
        self.assertFalse(derivations.reached(timezone.now() + timedelta(days=1)))


# One call of every derivation the host declares, over the seeded estate.
SAMPLES = {
    "estate.topology": lambda s: _of("topology", "_derive")(cli_principal()),
    "estate.relations": lambda s: _of("topology", "relation_graph")(principal=cli_principal()),
    "estate.findings": lambda s: _of("findings", "estate_findings")(principal=web_principal(s.user)),
    "estate.services": lambda s: _of("services", "_service_catalog")(),
    "attention.queue": lambda s: _of("domains", "_host_attention")(),
}


def _of(module: str, name: str):
    from importlib import import_module

    return getattr(import_module(f"hq.platform.application.{module}"), name)


def _clock_read_directly():
    """Stand in for ``timezone.now``: refuse a derivation's own code, serve Django's."""

    caller = sys._getframe(1)
    if caller.f_code.co_filename.endswith("django/utils/timezone.py"):
        caller = caller.f_back
    if derivations._FRAME.get() is not None and "/hq/" in caller.f_code.co_filename:
        raise AssertionError(
            f"{caller.f_code.co_filename}:{caller.f_lineno} reads the clock inside a "
            "derivation; ask hq.platform.application.derivations instead."
        )
    return derivations._clock()


class DeclaredDerivationTests(TestCase):
    """Every host derivation, held to what it declared."""

    @classmethod
    def setUpTestData(cls):
        cls.seeded = seed(0.05)

    def _run(self, name: str):
        with (
            mock.patch("hq.platform.application.domains.extension_domains", return_value=()),
            mock.patch("hq.platform.application.plugins.plugin_connection_specs", return_value=()),
            projection_scope(),
        ):
            return SAMPLES[name](self.seeded)

    def test_every_derivation_has_a_sample(self):
        for name in SAMPLES:
            self._run(name)
        host = {name for name in DERIVATIONS if not name.startswith("test.")}

        self.assertEqual(host, set(SAMPLES))

    def test_each_reads_only_the_tables_it_declared(self):
        derivations.UNDECLARED.clear()
        for name in SAMPLES:
            self._run(name)

        self.assertEqual(derivations.UNDECLARED, {})

    def test_each_reads_the_clock_only_through_the_primitive(self):
        with mock.patch("django.utils.timezone.now", _clock_read_directly):
            for name in SAMPLES:
                with self.subTest(derivation=name):
                    self._run(name)

    def test_each_answer_is_stored_and_served_back_equal(self):
        for name in SAMPLES:
            with self.subTest(derivation=name):
                first = self._run(name)
                with counting() as (ran, served):
                    second = self._run(name)
                self.assertEqual(ran, {})
                self.assertEqual(served[name], 1)
                self.assertEqual(second, first)
                pickle.dumps(first)

    def test_a_write_to_a_declared_table_derives_again(self):
        self._run("estate.topology")
        resource = ManagedResource.objects.order_by("key").first()
        ManagedResource.objects.filter(pk=resource.pk).update(enabled=not resource.enabled)

        with counting() as (ran, _served):
            self._run("estate.topology")

        self.assertEqual(ran["estate.topology"], 1)


class ActionItemCountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.seeded = seed(0.05)

    def setUp(self):
        self.client.force_login(self.seeded.user)
        patches = (
            mock.patch("hq.platform.application.domains.extension_domains", return_value=()),
            mock.patch("hq.platform.application.plugins.plugin_connection_specs", return_value=()),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.url = reverse("action_item_count")

    def test_an_unchanged_queue_is_answered_not_modified_without_composing_it(self):
        first = self.client.get(self.url)
        etag = first.headers["ETag"]

        with counting() as (ran, served), self.assertNumQueries(3):
            # The session, the person and the table revisions.
            again = self.client.get(self.url, headers={"if-none-match": etag})

        self.assertEqual((first.status_code, again.status_code), (200, 304))
        self.assertEqual((ran, served), ({}, {}))
        self.assertEqual(first.headers["Cache-Control"], "private, no-cache")

    def test_a_change_to_the_estate_is_answered_in_full(self):
        etag = self.client.get(self.url).headers["ETag"]
        ManagedResource.objects.update(enabled=False)

        again = self.client.get(self.url, headers={"if-none-match": etag})

        self.assertEqual(again.status_code, 200)
        self.assertNotEqual(again.headers["ETag"], etag)

    def test_setting_an_item_aside_is_answered_in_full(self):
        first = self.client.get(self.url)
        ActionItemRead.objects.create(
            user=self.seeded.user, key="any", revision="1", read_at=timezone.now()
        )

        again = self.client.get(self.url, headers={"if-none-match": first.headers["ETag"]})

        self.assertEqual(again.status_code, 200)

    def test_another_person_holds_another_validator(self):
        from django.contrib.auth import get_user_model

        etag = self.client.get(self.url).headers["ETag"]
        other = get_user_model().objects.create_superuser("other", "other@example.com", "x")
        self.client.force_login(other)

        self.assertEqual(
            self.client.get(self.url, headers={"if-none-match": etag}).status_code, 200
        )

    def test_a_validator_past_its_moment_is_answered_in_full(self):
        etag = self.client.get(self.url).headers["ETag"]
        lapsed = etag.rsplit("-", 1)[0] + '-1"'

        self.assertEqual(
            self.client.get(self.url, headers={"if-none-match": lapsed}).status_code, 200
        )
        self.assertEqual(
            self.client.get(self.url, headers={"if-none-match": '"anything"'}).status_code, 200
        )

    def test_a_queue_with_an_undeclared_source_carries_no_validator(self):
        from hq.platform.application.domains import Domain, all_domains
        from hq.platform.application.plugins import PluginIntegration

        extra = Domain(
            "example.extension", "Example", "extension", (),
            PluginIntegration(attention=lambda: ()),
        )
        with mock.patch(
            "hq.platform.application.domains.all_domains",
            return_value=(*all_domains(), extra),
        ):
            response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("ETag", response.headers)

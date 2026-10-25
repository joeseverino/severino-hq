"""The contention bench reads statements as the lock sees them."""

from collections import Counter

from django.test import SimpleTestCase

from hq.platform.core.management.commands.bench_contention import WRITE, RoleTimings, _percentile


class WriteTests(SimpleTestCase):
    def test_every_write_names_its_table(self):
        for sql, table in (
            ('INSERT INTO "hq_derived" ("cache_key") VALUES (%s)', "hq_derived"),
            ('INSERT OR REPLACE INTO "core_auditlog" VALUES (1)', "core_auditlog"),
            ('UPDATE "control_plane_providerconnection" SET "probed" = 1', "control_plane_providerconnection"),
            ('DELETE FROM "expenses_expense" WHERE "id" IN (1)', "expenses_expense"),
            ("REPLACE INTO hq_derived VALUES (1)", "hq_derived"),
        ):
            with self.subTest(sql=sql):
                self.assertEqual(WRITE.match(sql)[2], table)

    def test_a_read_or_a_begin_is_not_a_write(self):
        for sql in ('SELECT "id" FROM "expenses_expense"', "BEGIN IMMEDIATE", "PRAGMA busy_timeout = 0"):
            with self.subTest(sql=sql):
                self.assertIsNone(WRITE.match(sql))


class RoleTimingsTests(SimpleTestCase):
    def test_it_keeps_the_five_slowest_with_what_each_was(self):
        tally = RoleTimings()
        for took in range(10):
            tally.add(float(took), [], Counter(), what=f"/page/{took}/")

        self.assertEqual([what for _took, what in tally.slowest], [f"/page/{n}/" for n in (9, 8, 7, 6, 5)])
        self.assertEqual(_percentile(tally.times, 0.5), 4.0)
        self.assertEqual(_percentile([], 0.95), 0.0)

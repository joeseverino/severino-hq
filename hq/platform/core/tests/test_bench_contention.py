"""The contention bench reads statements as the lock sees them."""

from collections import Counter

from django.test import SimpleTestCase

from hq.platform.core.management.commands.bench_contention import RoleTimings, _percentile


class RoleTimingsTests(SimpleTestCase):
    def test_it_keeps_the_five_slowest_with_what_each_was(self):
        tally = RoleTimings()
        for took in range(10):
            tally.add(float(took), [], Counter(), what=f"/page/{took}/")

        self.assertEqual([what for _took, what in tally.slowest], [f"/page/{n}/" for n in (9, 8, 7, 6, 5)])
        # 0 to 9: the 50th percentile halfway between 4 and 5.
        self.assertEqual(_percentile(tally.times, 0.5), 4.5)
        self.assertEqual(_percentile([], 0.95), 0.0)

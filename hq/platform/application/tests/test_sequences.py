"""Every declared workflow walked through its examples (``sequences.SequenceSpec.examples``)."""

from itertools import groupby

from django.db import transaction
from django.test import TestCase

from hq.platform.core import seeds
from hq.platform.core.example_estate.testing import throwaway_media

from ..projection import projection_scope
from ..sequences import declared


class SequenceExampleTests(TestCase):
    def test_each_example_stands_at_its_step(self):
        """Each seed leaves its subject at the step its example names, in its state."""

        for spec in declared():
            by_seed = groupby(sorted(spec.examples.items(), key=lambda each: each[1].seed), lambda each: each[1].seed)
            for seed, examples in by_seed:
                with self.subTest(spec.name, seed=seed), transaction.atomic():
                    with throwaway_media(), seeds.built(seed), projection_scope():
                        for name, example in examples:
                            sequence = spec.of(spec.subject(example.key))
                            at = sequence.at
                            self.assertEqual(
                                (at.title, at.standing.state) if at else ("", ""),
                                (example.at.title if example.at else "", example.state),
                                f"{name}: {[(step.title, step.standing.label) for step in sequence.steps]}",
                            )
                    transaction.set_rollback(True)

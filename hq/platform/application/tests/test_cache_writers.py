"""Who writes the image layer cache: main alone, since the image Build publishes is
assembled from what it reads."""

import re

from django.test import SimpleTestCase, tag

from .test_pipeline import all_jobs, condition, label, script, steps


@tag("source")
class CacheWriterTests(SimpleTestCase):
    def test_the_layer_cache_is_written_only_from_the_default_branch(self):
        """A cache entry maps a build step to a layer on its writer's word, and
        the image Build publishes is assembled from what it reads."""
        writes = [
            (name, job, step)
            for name, _, job in all_jobs()
            for step in steps(job)
            if re.search(r"--cache-write|--cache-to", script(step))
        ]
        self.assertTrue(writes)
        for name, job, step in writes:
            with self.subTest(file=name, step=label(step)):
                # A conjunction, so neither term can be an alternative to a pull request.
                terms = {term.strip() for term in condition(job).split("&&")}
                self.assertNotIn("||", condition(job))
                self.assertLessEqual({"github.event_name == 'push'", "github.ref == 'refs/heads/main'"}, terms)

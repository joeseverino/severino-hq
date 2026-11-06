"""Who writes the caches CI reads: main alone, so a pull request adds no entries."""

import re

from django.test import SimpleTestCase, tag

from .test_pipeline import Node, all_jobs, composite_actions, condition, every_step, label, script, steps


def saves_a_cache(step: Node) -> bool:
    return step.get("uses", "").startswith("actions/cache/save@")


@tag("source")
class CacheWriterTests(SimpleTestCase):
    """A branch reads the default branch's caches and writes none, so a pull request adds no entries.

    zizmor's forbidden-uses refuses actions/cache, which saves on every run."""

    def test_a_cache_is_saved_only_from_the_default_branch(self):
        """A job that runs only on a push is on main: ci.yml pushes nowhere else."""
        saves = [(path, job, step) for path, _, job in all_jobs() for step in steps(job) if saves_a_cache(step)]
        self.assertGreater(len(saves), 1)
        for name, job, step in saves:
            with self.subTest(file=name, step=label(step)):
                where = f"{condition(job)} {condition(step)}"
                self.assertRegex(where, r"github\.event\.repository\.default_branch|github\.event_name == 'push'")

    def test_an_action_saves_a_cache_only_for_a_job_on_the_default_branch(self):
        """A composite action saves on the default branch, or when its caller
        passes ``save: true``, which only a job that runs on a push does."""
        saves = [
            (path.parent.name, step) for path in composite_actions() for step in every_step(path) if saves_a_cache(step)
        ]
        self.assertTrue(saves)
        for action, step in saves:
            with self.subTest(action=action):
                self.assertRegex(condition(step), r"github\.event\.repository\.default_branch|inputs\.save == 'true'")
        callers = [
            (path, job)
            for path, _, job in all_jobs()
            for step in steps(job)
            if step.get("with", {}).get("save") in (True, "true")
        ]
        self.assertTrue(callers)
        for name, job in callers:
            with self.subTest(file=name):
                self.assertRegex(condition(job), r"github\.event_name == 'push'")

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

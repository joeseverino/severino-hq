"""No task syncs the checkout's environment exactly.

`uv sync` removes every package its groups do not select (`uv run` adds and
removes none), so a task that synced the shared .venv to the default groups
would uninstall the fuzz and browser groups another task's `uv run --group`
installed, and that task, run beside it in one `mise run fast`, would fail
on a missing module. `--inexact` adds what is missing and removes nothing.
"""

import re

from django.test import SimpleTestCase, tag

from tests.mise_tasks import ROOT, body, mise_tasks, runs

# `uv sync` and its options, up to the end of the command.
SYNC = re.compile(r"\buv sync\b([^\n;&|]*)")
# A sync that leaves no group out removes nothing.
EXACT_IS_HARMLESS = ("--inexact", "--all-groups")
# A script that syncs another project's environment, from that project's directory, with why.
OTHER_PROJECT = {
    "tools/pipeline/check-plugin.sh": "it runs in an extension's checkout and syncs that package's own environment",
}


def exact_syncs(text: str) -> list[str]:
    """Each `uv sync` in ``text`` that removes what its groups leave out."""

    return [found[0] for found in SYNC.finditer(text) if not any(word in found[1] for word in EXACT_IS_HARMLESS)]


@tag("source")
class TaskSyncTests(SimpleTestCase):
    def test_no_task_syncs_the_environment_exactly(self) -> None:
        found = {}
        for name, task in mise_tasks().items():
            texts = {name: body(task)}
            texts |= {path.relative_to(ROOT).as_posix(): path.read_text() for path in runs(task)}
            for where, text in texts.items():
                if where not in OTHER_PROJECT and (syncs := exact_syncs(text)):
                    found[where] = syncs
        self.assertEqual(found, {}, "sync with --inexact, so the groups other tasks installed stay")

    def test_an_exact_sync_is_found_and_an_inexact_one_is_not(self) -> None:
        self.assertEqual(exact_syncs("set -eu\nuv sync --locked --quiet\n"), ["uv sync --locked --quiet"])
        self.assertEqual(exact_syncs("uv sync --locked --inexact --quiet && uv run x"), [])
        self.assertEqual(exact_syncs("uv sync --all-groups"), [])

"""Every file mise reads as a file task under mise-tasks/ is executable, so mise runs it."""

from pathlib import PurePosixPath

from django.test import SimpleTestCase, tag

from hq.platform.core.tests.task import names
from tests.mise_tasks import ROOT

TASKS = "mise-tasks"
EXECUTABLE = "100755"


def file_tasks(listing: list[str]) -> dict[str, str]:
    """Each file mise takes for a file task, by path, with the mode git checks it out with.

    mise walks the directory skipping any name that starts with a dot, reads a
    .toml file there as task definitions, and runs any other file only when it
    is executable: one that is not is hidden and cannot run (mise 2026.11.0,
    collect_task_files and load_tasks_includes in src/config/mod.rs)."""

    found = {}
    for record in listing:
        mode, _, path = record.partition(" ")
        parts = PurePosixPath(path).parts
        if path.endswith(".toml") or any(part.startswith(".") for part in parts):
            continue
        found[path] = mode
    return found


@tag("source", "tree")
class TaskFileModeTests(SimpleTestCase):
    def test_every_file_task_is_executable(self) -> None:
        tasks = file_tasks(names("ls-files", "--format=%(objectmode) %(path)", "--", TASKS, root=ROOT))
        self.assertGreater(len(tasks), 10)
        self.assertEqual(sorted(path for path, mode in tasks.items() if mode != EXECUTABLE), [])


@tag("source")
class TaskFileFilterTests(SimpleTestCase):
    def test_a_task_file_left_unexecutable_is_found_and_what_mise_never_runs_is_not(self) -> None:
        listing = [
            f"{EXECUTABLE} {TASKS}/turn",
            f"100644 {TASKS}/new",
            f"100644 {TASKS}/test.toml",
            f"100644 {TASKS}/.notes",
            f"100644 {TASKS}/.drafts/idea",
        ]
        self.assertEqual(file_tasks(listing), {f"{TASKS}/turn": EXECUTABLE, f"{TASKS}/new": "100644"})

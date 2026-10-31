"""Every task a tracked file names exists, and every CI gate job is named for
the task it runs, so a person, an agent or a small model types the obvious
word and gets the gate CI ran."""

import fnmatch
import re
import subprocess

from django.test import SimpleTestCase, tag

from hq.platform.application.tests.test_pipeline import ROOT, jobs, load, step_tasks, steps, workflow, workflows
from tests.mise_tasks import mise_tasks

# `mise run`, or `mise -C <dir> run`, then its own options and a task, to the
# end of the line or a closing backtick; `:::` starts the next task. A Python
# command line names the same words as a list of strings.
MISE_RUN = re.compile(r"\bmise(?:\s+-C\s+\S+)?\s+run(?=[ \t])([^\n`]*)")
MISE_RUN_LIST = re.compile(r'"mise",\s*"run"((?:,\s*"[^"\n]*")*)')
# mise run's options that take a value, which is no task.
OPTION_VALUES = {"-j", "--jobs", "-C", "--cd", "-o", "--output", "-s", "--shell", "-t", "--tool", "-E", "--env"}
# A word that stands for a task rather than naming one: a variable, a
# placeholder, a template.
STAND_IN = re.compile(r"[$%<>{}\[\]]|^$")
# Names in a fixture's own mise config, which no task of this checkout's is.
FIXTURE_TASKS = {
    # The throwaway configs the heavy turn's suite builds: a holder, and a
    # heavy task run from inside another, and two heavy tasks asked together.
    ("tests/shell/test-heavy.sh", "example-holder"),
    ("tests/shell/test-heavy.sh", "hang"),
    ("tests/shell/test-heavy.sh", "first"),
    ("tests/shell/test-heavy.sh", "second"),
    # The fixture config fast's tests run their gates from.
    ("tools/gate/tests/test_fast.py", "gate:*"),
}
# A word's quoting and the punctuation of the sentence around it.
QUOTED = re.compile(r"^[('\"\\]+|[.,;:)'\"\\]+$")


def tasks_named(words: list[str]) -> list[str]:
    """The task each run of a ``mise run`` command line names: the first word
    that is no option, and the first after each ``:::``."""

    named, expecting, value = [], True, False
    for word in (each if each == ":::" else QUOTED.sub("", each) for each in words):
        if value:
            value = False
        elif word == ":::":
            expecting = True
        elif not expecting:
            continue
        elif word.startswith("-"):
            value = word in OPTION_VALUES
        else:
            named.append(word)
            expecting = False
    return [name for name in named if not STAND_IN.search(name)]


def named_tasks(text: str) -> list[str]:
    """Every task a text names in a ``mise run`` command, shell's or Python's."""

    return [
        *(name for match in MISE_RUN.finditer(text) for name in tasks_named(match[1].split())),
        *(name for match in MISE_RUN_LIST.finditer(text) for name in tasks_named(re.findall(r'"([^"]*)"', match[1]))),
    ]


def tracked_texts() -> list[tuple[str, str]]:
    """Every file git tracks that reads as text, by path."""

    listed = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True, text=True).stdout
    texts = []
    for name in filter(None, listed.split("\0")):
        path = ROOT / name
        if path.is_file() and not path.is_symlink():
            try:
                texts.append((name, path.read_text()))
            except UnicodeDecodeError:
                continue
    return texts


def a_task(name: str, tasks: set[str]) -> bool:
    """Whether ``name`` is a task, or a pattern that reaches one: mise's ``*`` stays within one ``:`` group."""

    if not any(mark in name for mark in "*?"):
        return name in tasks
    pattern = re.compile(fnmatch.translate(name).replace(".*", "[^:]*"))
    return any(pattern.fullmatch(task) for task in tasks)


# The jobs that run no task of their own: the proof a push stands on, the cache
# writer, the composition and the two verdicts. Scan runs CodeQL's own action,
# which uploads what it finds, and is named for the task that runs it locally.
NOT_TASK_JOBS = {"proven", "caches", "compose", "ready", "release"}
# A second job of a task: a pull request's image, verified from the registry
# beside its composition.
SECOND_JOBS = {"build-verify": "build"}


# The tasks CI's gate jobs are named for, each the gates of its job.
GATE_JOBS = {"lint", "test", "browser", "build", "scan"}


@tag("source", "tree")
class TaskReferenceTests(SimpleTestCase):
    def test_every_task_a_tracked_file_names_exists(self):
        """A task renamed or retired leaves no command that names it: in a
        workflow, a doc, a message, a test or a script."""
        tasks = set(mise_tasks())
        named = [(path, name) for path, text in tracked_texts() for name in named_tasks(text)]
        self.assertGreater(len(named), 100)
        dead = sorted({(path, name) for path, name in named if not a_task(name, tasks)} - FIXTURE_TASKS)
        self.assertEqual(dead, [], "each names a task that does not exist")


@tag("source")
class CommandLineTests(SimpleTestCase):
    def test_a_command_line_names_each_task_it_runs(self):
        self.assertEqual(
            named_tasks("run `mise run -c -j 1 lint ::: 'test:*' ::: browser layout-1`."), ["lint", "test:*", "browser"]
        )
        self.assertEqual(named_tasks('mise -C "${repo}" run -q fast base\nmise run "$GATE"'), ["fast"])
        self.assertEqual(named_tasks('["mise", "run", "-c", "lint", ":::", "test", "--composed"]'), ["lint", "test"])
        self.assertEqual(named_tasks("`mise run` takes one task; `mise run <task>`"), [])
        self.assertTrue(a_task("lint:*", {"lint:ruff"}))
        self.assertFalse(a_task("lint:*", {"lint:a:b"}))
        self.assertFalse(a_task("checks:*", {"lint:ruff"}))


@tag("source")
class GateJobNameTests(SimpleTestCase):
    def test_every_gate_job_is_named_for_the_task_it_runs(self):
        """A person, an agent or a small model reading a failed job runs the task
        of its name, and gets the gate CI ran."""
        tasks = mise_tasks()
        checked = 0
        for path in workflows():
            for name, job in jobs(load(path)).items():
                ran = {
                    task
                    for step in steps(job)
                    for pattern in step_tasks(step)
                    for task in tasks
                    if pattern.fullmatch(task)
                }
                gates = {task.split(":")[0] for task in ran} & GATE_JOBS
                if not gates:
                    continue
                checked += 1
                with self.subTest(workflow=path.name, job=name):
                    self.assertEqual(gates, {SECOND_JOBS.get(name, name)})
        self.assertGreater(checked, 5)
        ci = jobs(workflow("ci.yml"))
        self.assertEqual(set(ci) - NOT_TASK_JOBS - set(SECOND_JOBS), GATE_JOBS)

    def test_compose_waits_on_the_job_that_builds_the_host_image_by_its_name(self):
        """GitHub's API names a job by its display name, so a renamed build job
        would leave Compose waiting on a job that never runs."""
        reader = (ROOT / "tools" / "pipeline" / "host-image.sh").read_text()
        (named,) = re.findall(r'select\(\.name == "([^"]+)"\) \| \(\.conclusion', reader)
        self.assertEqual(named, jobs(workflow("ci.yml"))["build"]["name"])

"""HQ's test runner, the one entry every test passes through: what a run
selects, and what each test process sets up before its first test.

Its parts live beside it: the stall watch (``stalls``), what a run reports
(``run_report``) and the parallel workers' database files (``file_clones``).
"""

import contextlib
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import unittest
from contextvars import ContextVar
from pathlib import Path
from typing import override
lazy import inspect
lazy import pdb

from django.conf import settings
from django.db import connections
from django.db.models.signals import post_save
from django.test.runner import DiscoverRunner, ParallelTestSuite, _init_worker
lazy from django.apps import apps
lazy from django.contrib.auth import get_user_model
lazy from django.core.management.base import BaseCommand
lazy from django.db.backends.sqlite3.creation import DatabaseCreation
lazy from django.test import TransactionTestCase

from hq.config.imports import lazily
from hq.config.sandbox import shared_tempdir
from hq.platform.core.file_clones import clone_for_workers, use_the_file_clone
from hq.platform.core.isolation_testing import own_markers, patches_leave_no_copy
from hq.platform.core.run_report import (
    ClassTimedSuite,
    CompositionRemoteRunner,
    CompositionTextResult,
    report_class_times,
    write_failures,
)
from hq.platform.core.stalls import stall_watch
lazy from hq.platform.application import plugins, roles
lazy from hq.platform.core import append_only
lazy from hq.platform.core.attribution import bind_this_thread, bound, command_operation, from_the_shell


def performance_cores() -> int | None:
    """Performance cores on Apple silicon (``hw.perflevel0``); None elsewhere."""

    if sys.platform != "darwin":
        return None
    try:
        found = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "hw.perflevel0.physicalcpu"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except OSError, subprocess.CalledProcessError:
        return None
    return int(found) if found.isdigit() else None


# One test worker per performance core: a worker on an efficiency core slows
# every test it takes, and extra workers push a small machine into swap. Set at
# import, before Django reads DJANGO_TEST_PROCESSES; a value already set, or an
# explicit --parallel, wins.
if (cores := performance_cores()) is not None:
    os.environ.setdefault("DJANGO_TEST_PROCESSES", str(cores))


# Every user a test makes operates HQ, as every account did before a sign-in
# resolved roles, so a test of something else need not give one. A test of how
# a person comes to hold a role, or holds none, makes its users inside
# ``no_default_role``.
_default_role = ContextVar("hq_test_default_role", default=True)


@contextlib.contextmanager
def no_default_role():
    """Users made in the block hold only the role HQ itself gives them."""

    with _default_role.set(False):
        yield


def _operator_by_default(instance, created, *, raw=False, **kwargs):
    if created and not raw and _default_role.get() and isinstance(instance, get_user_model()):
        roles.give_operator(instance)


def default_people_to_operator() -> None:
    """In this process, a user a test makes holds the operator role (``no_default_role``)."""

    post_save.connect(_operator_by_default, dispatch_uid="hq.tests.operator_by_default")


# The interface of what a test writes outside every door. Named only here and in
# tests, so no product code can take a test's binding for a door's
# (tools/lint/ast-grep/rules/test-interface-bound-only-in-tests.yml).
TEST_INTERFACE = "test"


def bind_test_arrangement() -> None:
    """In this process, what a test writes outside a door is the test's own.

    A fixture, a sign-in or a direct call to a service runs on the test's
    thread outside every door, so this one binding stands for all of them. A
    door still binds its own inside it, and a thread a door starts inherits
    none of it, so a door that does not bind is still refused there.
    """

    bind_this_thread(interface=TEST_INTERFACE, actor="", operation="test")


def flush_through_the_audit_guard() -> None:
    """In this process, a TransactionTestCase's flush empties the audit log
    with its guard lifted, and installs the guard again after it.

    The flush is a ``DELETE`` of every row, which the guard refuses everywhere
    else, a production flush included.
    """

    # Django offers no hook before a test's flush, so the flush itself is wrapped.
    flush = TransactionTestCase._fixture_teardown
    if getattr(flush, "through_the_audit_guard", False):
        return

    def _fixture_teardown(self):
        with contextlib.ExitStack() as lifted:
            for alias in self._databases_names(include_mirrors=False):
                lifted.enter_context(append_only.lifted(connections[alias]))
            flush(self)

    _fixture_teardown.through_the_audit_guard = True
    TransactionTestCase._fixture_teardown = _fixture_teardown


def commands_from_the_shell() -> None:
    """In this process, a management command a test starts runs as ``manage.py``
    runs it: inside the shell's door, as the local operator.

    So a command that binds someone else inside it is refused here, as it is in
    production. A command started inside a door that has bound its own (a job,
    another command) keeps that door's binding, as it does in production.
    """

    # Django offers no hook before a command runs; call_command and
    # run_from_argv both pass through BaseCommand.execute, so it is wrapped.
    execute = BaseCommand.execute
    if getattr(execute, "from_the_shell", False):
        return

    def _execute(self, *args, **options):
        here = bound()
        if here is None or here.interface != TEST_INTERFACE or here.actor:
            return execute(self, *args, **options)
        named = type(self).__module__.rpartition(".")[2]
        with from_the_shell(command_operation(["manage.py", named])):
            return execute(self, *args, **options)

    _execute.from_the_shell = True
    BaseCommand.execute = _execute


# Outside the checkout. Hypothesis caches constants it finds in every module it
# can import, installed extensions included, and a cache in the tree is text
# the host must never carry. Outside the run's own temporary directory too, so
# a failure it found replays in the next run.
HYPOTHESIS_HOME = shared_tempdir() / "severino-hq-hypothesis"


def hypothesis_profiles() -> None:
    """In this process, every property runs under one profile: ``dev`` here,
    Hypothesis's own ``ci`` where it detects CI, ``HYPOTHESIS_PROFILE`` over both.

    ``ci`` keeps what Hypothesis's built-in one sets (derandomized, no database,
    the failing blob printed) and runs more of each. ``dev`` stays light enough
    for ``fast`` and replays a failure it found from its database.
    """

    try:
        from hypothesis import settings as hypothesis
        from hypothesis.configuration import set_hypothesis_home_dir
        from hypothesis.database import DirectoryBasedExampleDatabase
    except ImportError:
        return
    set_hypothesis_home_dir(HYPOTHESIS_HOME)
    hypothesis.register_profile(
        "dev",
        max_examples=25,
        stateful_step_count=20,
        deadline=None,
        database=DirectoryBasedExampleDatabase(HYPOTHESIS_HOME / "examples"),
    )
    hypothesis.register_profile("ci", hypothesis.get_profile("ci"), max_examples=200, stateful_step_count=50)
    ci = hypothesis.get_current_profile_name() == "ci"
    hypothesis.load_profile(os.environ.get("HYPOTHESIS_PROFILE") or ("ci" if ci else "dev"))


def breakpoint_on_the_terminal() -> None:
    """``breakpoint()`` in a test: pdb on the process's own stdout, since
    --buffer holds the test's ``sys.stdout``, where the prompt would wait
    unseen."""

    debugger = pdb.Pdb(stdout=sys.__stdout__)
    # Without pyrepl (no terminal), pdb reads through input(), which prompts on sys.stdout.
    debugger.use_rawinput = False
    debugger.set_trace(inspect.currentframe().f_back)


def prepare_this_process() -> None:
    """What every test process sets up before its first test: the run's
    parent, and each parallel worker as it starts."""

    own_markers()
    patches_leave_no_copy()
    default_people_to_operator()
    flush_through_the_audit_guard()
    commands_from_the_shell()
    bind_test_arrangement()
    hypothesis_profiles()


def _init_worker_on_file_databases(counter, *args, **kwargs):
    """Django's worker startup, with SQLite kept on disk and imports lazy.

    Both take effect here rather than at import because a worker under
    ``spawn`` or ``forkserver`` starts from a fresh interpreter, which never
    ran ``manage.py``'s ``main()``. Lazy before ``django.setup()``, so a worker
    holds what its tests reach, as the process that started the run does.
    """

    lazily()
    DatabaseCreation.setup_worker_connection = use_the_file_clone
    stall_watch.workers = counter
    started = _init_worker(counter, *args, **kwargs)
    prepare_this_process()
    return started


class FileClonedParallelSuite(ParallelTestSuite):
    # A worker's traceback reaches the parent through tblib; without it the error
    # reads "cannot pickle 'traceback' object", so run --parallel=1 or install it.
    # A plain function, not a staticmethod: Django reads `self.init_worker
    # .__func__`, which only exists on a bound method.
    init_worker = _init_worker_on_file_databases
    runner_class = CompositionRemoteRunner


# The deployment identity the suite runs under, whatever the shell exports: the
# values CI sets. A developer's `mise.local.toml` names a real host, and HQ derives its
# own hostname from it, which changes what pages read.
NEUTRAL_IDENTITY = {
    "DJANGO_ALLOWED_HOSTS": "127.0.0.1,testserver",
    "DJANGO_CSRF_TRUSTED_ORIGINS": "",
    "SEVERINO_SITE_HOST": "",
}


def plugin_test_modules() -> list[str]:
    """The module of each installed plugin's Django app, where its tests live."""

    configs = {config.name: config.name for config in apps.get_app_configs()}
    configs |= {f"{type(c).__module__}.{type(c).__qualname__}": c.name for c in apps.get_app_configs()}
    return [configs.get(entry, entry) for entry in plugins.installed_plugin_apps()]


def tests_in(suite: unittest.TestSuite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from tests_in(test)
        else:
            yield test


# The browser gates' package: a test whose class comes from it drives a browser.
BROWSER_TESTS = "hq.platform.core.tests.browser"


def drives_a_browser(test: unittest.TestCase) -> bool:
    return any(
        kind.__module__ == BROWSER_TESTS or kind.__module__.startswith(f"{BROWSER_TESTS}.")
        for kind in type(test).__mro__
    )


def browser_failures() -> Path:
    """Where a browser run keeps the shots of what failed: in the checkout,
    since they outlive the run, in a folder of the task that ran it, so each
    leg of ``mise run browser`` keeps its own."""

    task = os.environ.get("MISE_TASK_NAME") or "test"
    return Path(settings.BASE_DIR, "var", "browser-failures", re.sub(r"[^\w.-]", "-", task))


def require_tests_from(suite: unittest.TestSuite, modules: list[str]) -> None:
    """Refuse a run in which an installed plugin contributed no test."""

    ids = [test.id() for test in tests_in(suite)]
    silent = [module for module in modules if not any(i.startswith(module + ".") for i in ids)]
    if silent:
        raise RuntimeError(f"{len(silent)} installed plugin(s) contributed no test to this run.")


# Tests that read the repository rather than run the product: the docs, the
# workflows, the Dockerfile, the source itself. They hold for a checkout, so
# they run once, on one (`mise run lint:source`), and never in an image,
# which ships none of what they read.
SOURCE = "source"


def excluded_tags(labels: list[str] | None, tags: set[str], exclude: set[str]) -> set[str]:
    """The tags a run leaves out: a whole-suite run leaves out the source
    tests unless it asks for them. A named label runs what it names."""

    if labels or SOURCE in tags:
        return exclude
    return exclude | {SOURCE}


class SeverinoTestRunner(DiscoverRunner):
    """Django's runner, with parallel workers on checkpointed database files."""

    parallel_test_suite = FileClonedParallelSuite
    test_suite = ClassTimedSuite

    def __init__(self, *args, installed_plugins=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.installed_plugins = installed_plugins

    @classmethod
    def add_arguments(cls, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "--installed-plugins",
            action="store_true",
            help="Run every installed plugin's tests and none of the host's.",
        )
        # A run's output is its failures: Django's --buffer drops what a passing
        # test prints and prints a failing one's with it, and verbosity 0 its
        # progress (the dots, the test databases); -v 1 shows that again.
        parser.set_defaults(buffer=True, verbosity=0)
        parser.add_argument(
            "--no-buffer", dest="buffer", action="store_false", help="Print what passing tests print too."
        )

    def setup_test_environment(self, **kwargs):
        # Environment first, so spawned workers load the same settings.
        os.environ.update(NEUTRAL_IDENTITY)
        settings.ALLOWED_HOSTS = ["127.0.0.1", "testserver"]
        settings.CSRF_TRUSTED_ORIGINS = []
        settings.SEVERINO_SITE_HOST = ""
        super().setup_test_environment(**kwargs)
        sys.breakpointhook = breakpoint_on_the_terminal
        prepare_this_process()

    def load_tests_for_label(self, label, discover_kwargs):
        # Discovery loads a package's __init__ as well as its test files, so a
        # browser gate declared there is found; the browser gates run by name.
        # Left out here, as each label loads: Django then splits what is left
        # across the workers, and a suite rebuilt after that runs in one process.
        tests = super().load_tests_for_label(label, discover_kwargs)
        if self.named:
            return tests
        return self.test_suite(test for test in tests_in(tests) if not drives_a_browser(test))

    def build_suite(self, test_labels=None, **kwargs):
        # A run with no labels is the whole suite: the host's and every
        # installed plugin's, so a composed run tests what it composes. Named
        # labels run what they name.
        self.named = bool(test_labels)
        self.exclude_tags = excluded_tags(test_labels, self.tags, self.exclude_tags)
        if test_labels:
            suite = super().build_suite(test_labels, **kwargs)
        else:
            modules = plugin_test_modules()
            if self.installed_plugins and not modules:
                raise RuntimeError("--installed-plugins: no plugin is installed.")
            suite = super().build_suite([*modules] if self.installed_plugins else [".", *modules], **kwargs)
            require_tests_from(suite, modules)
        if any(drives_a_browser(test) for test in tests_in(suite)):
            # Here, in the process that starts the run and before any worker
            # shoots: the last run's shots of this task give way to this one's.
            shutil.rmtree(browser_failures(), ignore_errors=True)
        if self.tags and next(tests_in(suite), None) is None:
            # A gate that selects by tag and finds nothing has checked nothing.
            untagged = f" Their tests run with `mise run test {' '.join(test_labels)}`." if test_labels else ""
            raise RuntimeError(f"No test carries the tag(s) {', '.join(sorted(self.tags))}.{untagged}")
        return suite

    def get_resultclass(self):
        return super().get_resultclass() or CompositionTextResult

    @override
    def run_suite(self, suite, **kwargs):
        began_load = os.getloadavg()
        try:
            result = super().run_suite(suite, **kwargs)
        except multiprocessing.TimeoutError as stopped:
            # Django's parent stops on a negative worker count, which a worker
            # also sets when it exits on a stall (StallWatch.end).
            stopped.add_note("Or a worker exited on a test that did not stop when interrupted: see its stall report.")
            raise
        report_class_times(result, sys.stderr, began_load)
        write_failures(result)
        return result

    def setup_databases(self, **kwargs):
        # Build the database first and clone it second, rather than letting one
        # call do both, because the checkpoint has to happen between the two.
        requested = self.parallel
        self.parallel = 0
        try:
            config = super().setup_databases(**kwargs)
        finally:
            self.parallel = requested
        if requested > 1:
            clone_for_workers(config, requested, verbosity=self.verbosity, keepdb=self.keepdb)
        return config

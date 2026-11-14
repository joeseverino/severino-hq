"""dev:look split among processes: each page and width is looked at by exactly
one share, what every share found is said once, in the order the pages were
asked, a share that ends without its findings fails the run naming its pages,
and the processes never take more of the heavy turn than the machine holds."""

import io
import os
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, tag

from . import look

PAGES = [f"example/page-{number}" for number in range(47)]
WIDTHS = "402,1360"
ASKED = {"usage_pages": "all", "usage_rule": "survey", "usage_widths": WIDTHS}
FINDING = "sideways: .example-planted is 40px wider than its row"


class _Share:
    """One process's look, with the browser taken out: every page holds but
    the one ``planted``, where the rule finds ``FINDING``."""

    test_look = look.Look.test_look
    look_at_every_page = look.Look.look_at_every_page
    base, base_errors, base_unfit, base_kept, undrawn = None, {}, "", [], ()
    tree, tree_store = "", Path()

    def __init__(self, at, planted, shares=()):
        from hq.platform.core.tests import look_shares

        self.dealt, self.share_at, self.planted = PAGES, at, planted
        self.asked = look_shares.mine(PAGES, at)
        self.shares = list(shares)

    def __getattr__(self, name):
        return getattr(look.Look, name).__get__(self)

    def draw_with_script(self, _scripted):
        pass

    def refusals_so_far(self):
        return []

    def look(self, name, width, _rule):
        self.judged_at[name, width] = None
        print(f"{name} at {width}: drawn")
        return [FINDING] if name == self.planted else []

    def fail(self, said):
        raise AssertionError(said)


def _ended(index):
    """A share's process that has ended, its lines all said."""

    return SimpleNamespace(
        index=index, process=SimpleNamespace(wait=lambda: 0), relay=SimpleNamespace(join=lambda: None)
    )


@tag("source")
class SharesTests(SimpleTestCase):
    total = 3

    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(look, "this_run", lambda: self.folder))
        self.enterContext(mock.patch.object(look, "changed_since", lambda *_: []))
        self.enterContext(mock.patch.object(look, "_commit", lambda ref: ref))

    def share(self, index, planted):
        """Share ``index``'s process, as the run that asked starts it."""

        from hq.platform.core.tests import look_shares

        given = {**ASKED, look_shares.FOLDER: str(self.folder), look_shares.SHARE: f"{index}/{self.total}"}
        with mock.patch.dict(os.environ, given, clear=True), mock.patch("sys.stdout", new_callable=io.StringIO):
            _Share((index, self.total), planted).test_look()

    def asker(self, planted, ran=None):
        """The run that asked, sharing with every other share, which ran
        before it (``ran``, all of them by default): what it printed, and
        what it failed on."""

        ran = range(2, self.total + 1) if ran is None else ran
        for index in ran:
            self.share(index, planted)
        shares = [_ended(index) for index in range(2, self.total + 1)]
        with (
            mock.patch.dict(os.environ, ASKED, clear=True),
            mock.patch("sys.stdout", new_callable=io.StringIO) as printed,
            self.assertRaises(AssertionError) as failed,
        ):
            _Share((1, self.total), planted, shares).test_look()
        return printed.getvalue(), str(failed.exception)

    def test_a_finding_on_a_page_of_a_later_share_is_said_once_and_every_page_once_in_the_order_asked(self):
        from hq.platform.core.tests import look_shares

        planted = look_shares.mine(PAGES, (self.total, self.total))[-1]
        self.assertNotIn(planted, look_shares.mine(PAGES, (1, self.total)))
        printed, failed = self.asker(planted)
        widths = [int(width) for width in WIDTHS.split(",")]
        began = [line.removeprefix("Looking at ") for line in printed.splitlines() if line.startswith("Looking at ")]
        self.assertEqual(began, [f"{name} at {width}" for name in PAGES for width in widths])
        # What each page said while it was looked at, under it.
        for name in PAGES:
            for width in widths:
                self.assertEqual(printed.count(f"{name} at {width}: drawn"), 1)
        report = (self.folder / look.REPORT).read_text(encoding="utf-8")
        self.assertEqual(report.count(FINDING), len(widths))
        self.assertIn(f"fails on 1 of {len(PAGES)} pages", failed)
        named = [line for line in failed.splitlines() if FINDING in line]
        self.assertEqual([line.partition(":")[0] for line in named], [f"{planted} at {width}" for width in widths])
        self.assertIn(look.LOOKED_IN.format(self.total, "processes"), printed.splitlines())

    def test_a_share_that_ends_without_its_findings_fails_the_run_naming_its_pages(self):
        from hq.platform.core.tests import look_shares

        planted = look_shares.mine(PAGES, (1, self.total))[0]
        look_shares.log(self.folder, 2).write_text("Looking\nRuntimeError: the browser closed\n", encoding="utf-8")
        _printed, failed = self.asker(planted, ran=[3])
        [said] = [line for line in failed.splitlines() if line.startswith("share 2/")]
        self.assertIn("RuntimeError: the browser closed", said)
        self.assertEqual(said.rpartition(": ")[2].split(", "), look_shares.mine(PAGES, (2, self.total)))


@tag("source")
class ShareRunsTheLookTests(SimpleTestCase):
    def test_every_shares_process_runs_the_look_whatever_share_ci_names(self):
        """The test runner leaves out a browser test that CI's share does not
        pick, and a share's process is the same test run."""

        from hq.platform.core import test_runner
        from hq.platform.core.tests import look_shares

        test = look.Look("test_look")
        for total in range(2, 9):
            for index in range(2, total + 1):
                env = look_shares.environment(
                    {**ASKED, "HQ_BROWSER_SHARD": "1/7"}, (index, total), Path("var/look"), Path("var/test.sqlite3")
                )
                with self.subTest(share=f"{index}/{total}"), mock.patch.dict(os.environ, env, clear=True):
                    self.assertTrue(test_runner.in_this_share(test))
                    self.assertEqual(look_shares.this_share(), (index, total))


@tag("source")
class SaidWholeTests(SimpleTestCase):
    def test_every_pages_lines_wait_for_room_in_a_pipe_left_non_blocking(self):
        """Playwright's driver shares the run's pipe and leaves it non-blocking,
        where stdout's unbuffered text layer drops what a full pipe refuses."""

        from hq.platform.core.tests import look_shares

        printed = {
            (f"example/page-{number}", 402): [f"Looking at example/page-{number} at 402"] for number in range(50)
        }
        read, write = os.pipe()
        heard = bytearray()
        reader = threading.Thread(target=lambda: heard.extend(b"".join(iter_read(read))))
        reader.start()
        # As PYTHONUNBUFFERED leaves stdout: text written straight through to the descriptor.
        with io.TextIOWrapper(io.FileIO(write, "w"), write_through=True) as stream:
            os.set_blocking(write, False)
            with mock.patch("sys.stdout", stream), mock.patch("sys.stderr", io.StringIO()):
                look_shares.say(printed)
            self.assertTrue(os.get_blocking(write))
        reader.join()
        os.close(read)
        self.assertEqual(heard.decode().splitlines(), [line for lines in printed.values() for line in lines])


def iter_read(descriptor):
    while chunk := os.read(descriptor, 4096):
        yield chunk


@tag("source")
class MergedTests(SimpleTestCase):
    def test_a_page_no_share_or_two_shares_looked_at_is_named(self):
        from hq.platform.core.tests.look_shares import Looked, merged

        one = Looked(found={("example/one", 402): [], ("example/two", 402): []})
        two = Looked(found={("example/two", 402): []})
        whole, wrong = merged(["example/one", "example/two", "example/three"], [402], [one, two])
        self.assertEqual(list(whole.found), [("example/one", 402), ("example/two", 402)])
        self.assertEqual(
            wrong,
            [
                "example/two at 402 was looked at by more than one share",
                "example/three at 402 was looked at by no share",
            ],
        )

    def test_what_a_share_writes_reads_back_whole(self):
        from hq.platform.core.tests.look_shares import Looked

        key = ("example/one", 402)
        written = Looked(
            found={key: [FINDING]},
            measured={key: {"eval": "Example"}},
            errors={key: ["console: example"]},
            judged=[key],
            printed={key: ["Looking at example/one at 402"]},
            unanswered={"example/two": 404},
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "share-2.json")
            written.write(path)
            self.assertEqual(Looked.read(path), written)


# The heavy turn is development code, which no image ships.
@tag("source")
class ProcessesTests(SimpleTestCase):
    def test_the_processes_take_no_more_of_the_turn_than_the_machine_holds_and_one_for_a_few_pages(self):
        from hq.platform.core.tests import look_shares
        from tools.gate import turn  # noqa: TID251 - the budget the processes are held to

        for cores, memory in ((8, 8 * 2**30), (12, 30 * 2**30), (2, 4 * 2**30), (1, 2**30)):
            budget = turn.capacity(cores, memory)
            for pages in (1, look_shares.PAGES_EACH, 308, 10_000):
                with self.subTest(cores=cores, memory=memory, pages=pages):
                    processes = look_shares.processes(pages, budget)
                    self.assertGreaterEqual(processes, 1)
                    self.assertLessEqual(look_shares.tokens(processes), max(budget, turn.BROWSER))
        self.assertEqual(look_shares.processes(look_shares.PAGES_EACH - 1, 12), 1)
        # An 8 GB Mac's four tokens are two browsers; the laptop's twelve, six.
        self.assertEqual(look_shares.processes(308, turn.capacity(8, 8 * 2**30)), 2)
        self.assertEqual(look_shares.processes(308, turn.capacity(12, 30 * 2**30)), 6)

    def test_a_run_takes_as_many_processes_as_the_share_it_holds(self):
        from hq.platform.core.tests import look_shares
        from tools.gate import turn  # noqa: TID251 - the share the run holds

        with mock.patch.dict(os.environ, {turn.SHARE: "4"}, clear=True):
            self.assertEqual(look_shares.granted(), 4)
        with mock.patch.dict(os.environ, {turn.HELD: f"{turn.GATE},{turn.LOCK}"}, clear=True):
            self.assertEqual(look_shares.granted(), turn.CAPACITY)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(look_shares.granted(), turn.BROWSER)

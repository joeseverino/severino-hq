"""The dev tools' one reader of a task's flags and of the pages asked, and their throwaway worktree of a ref."""

import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, tag
from django.urls import reverse

from hq.platform.core.tests import task
from hq.platform.core.tests.task import PageRefused, RefError, Usage


@tag("source")
class UsageTests(SimpleTestCase):
    def test_flags_read_as_mise_exports_them(self):
        # As mise exports `--dark --keep 'a b' --keep "c'd"` with --base unset and --seed given empty.
        given = Usage.from_environment(
            {
                "usage_dark": "true",
                "usage_perf": "false",
                "usage_keep": "'a b' 'c'\\''d'",
                "usage_seed": "",
                "PATH": "/",
            }
        )

        self.assertTrue(given.switch("dark"))
        self.assertFalse(given.switch("perf"))
        self.assertEqual(given.repeated("keep"), ("a b", "c'd"))
        self.assertEqual(given.repeated("pages"), ())
        self.assertTrue(given.given("seed"))
        self.assertFalse(given.given("base"))
        self.assertEqual(given.text("base", "main"), "main")
        self.assertFalse(given.given("PATH"))


@tag("source")
class PagesTests(SimpleTestCase):
    EVERY = ["/", "/contacts/"]

    def test_a_page_is_an_estate_name_a_path_or_a_route_name_each_as_its_address_once(self):
        containers = reverse("control_plane:containers")
        asked = [f"estate{containers}", containers, "control_plane:containers", "estate/contacts?kind=x", "estate"]

        self.assertEqual(task.pages(asked, self.EVERY), [containers, "/contacts/?kind=x", "/"])

    def test_all_is_every_page_the_navigation_lists_and_every_page_known_by_name(self):
        self.assertEqual(task.pages(["estate/all"], self.EVERY, ["example"]), self.EVERY)
        self.assertEqual(task.pages(["all"], self.EVERY, ["example"]), ["example", *self.EVERY])
        self.assertEqual(task.pages(["example"], self.EVERY, ["example"]), ["example"])

    def test_every_name_that_is_no_page_is_refused_together_with_the_nearest_routes(self):
        with self.assertRaises(PageRefused) as refused:
            task.pages(["estate/containers", "control_plane:containerz", "example"], self.EVERY)
        said = str(refused.exception)

        nearest = f"control_plane:containers ({reverse('control_plane:containers')})"
        self.assertIn(f"No page estate/containers; the nearest: {nearest}", said)
        self.assertIn(f"No page control_plane:containerz; the nearest: {nearest}", said)
        self.assertIn("No page example", said)

    def test_an_estate_page_keeps_its_query_and_its_path_its_slash(self):
        self.assertEqual(task.estate_path("estate/audit/?awaiting=1"), "/audit/?awaiting=1")
        self.assertEqual(task.estate_path("estate/audit?awaiting=1"), "/audit/?awaiting=1")
        self.assertEqual(task.estate_path("estate/audit"), "/audit/")
        self.assertEqual(task.estate_path("estate"), "/")

    def test_a_page_known_by_name_is_named_as_nearest_where_it_is_one(self):
        with self.assertRaisesMessage(PageRefused, "the nearest: dense/services"):
            task.pages(["dense/service"], self.EVERY, ["dense/services"])


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout


def _repository(folder: Path) -> Path:
    """A repository of one commit, made with no identity or hook of the machine's."""

    _git("init", "--quiet", cwd=folder)
    (folder / "page.txt").write_text("example\n", encoding="utf-8")
    _git("add", "page.txt", cwd=folder)
    _git(
        "-c", "user.name=Example", "-c", "user.email=example@example.com", "-c", "commit.gpgsign=false",
        "commit", "--quiet", "--no-verify", "-m", "example", cwd=folder,
    )  # fmt: skip
    return folder


@tag("source")
class WorktreeTests(SimpleTestCase):
    def test_the_worktree_and_its_folder_are_gone_however_the_block_ends(self):
        with tempfile.TemporaryDirectory() as folder:
            checkout = _repository(Path(folder))
            with self.assertRaises(KeyError), task.worktree("HEAD", checkout) as tree:
                self.assertTrue((tree / "page.txt").is_file())
                raise KeyError("the run broke")
            self.assertFalse(tree.parent.exists())
            self.assertNotIn(str(tree), _git("worktree", "list", "--porcelain", cwd=checkout))

    def test_a_ref_that_names_nothing_is_said_and_leaves_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            checkout = _repository(Path(folder))
            with self.assertRaisesMessage(RefError, "example-missing"):
                task.commit_of("example-missing", checkout)
            with self.assertRaises(RefError), task.worktree("example-missing", checkout):
                self.fail("a worktree of nothing")
            self.assertEqual(_git("worktree", "list", "--porcelain", cwd=checkout).count("worktree "), 1)

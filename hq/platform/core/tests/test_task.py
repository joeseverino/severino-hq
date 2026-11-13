"""The dev tools' one reader of a task's flags and of the pages asked, and their throwaway worktree of a ref."""

import subprocess
import tempfile
from pathlib import Path
from unittest import mock

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

    def test_all_is_every_page_of_the_estate_and_every_page_known_by_name(self):
        self.assertEqual(task.pages(["estate/all"], self.EVERY, ["example"]), self.EVERY)
        self.assertEqual(task.pages(["all"], self.EVERY, ["example"]), ["example", *self.EVERY])
        self.assertEqual(task.pages(["example"], self.EVERY, ["example"]), ["example"])

    def test_all_leaves_out_a_record_page_the_estate_drawn_does_not_hold(self):
        held, missing = reverse("contacts:detail", args=[1]), reverse("contacts:detail", args=[2])

        class Estate:
            def linked(self):
                return [held]

            def at(self, address):
                return task.Drawn() if address == held else None

        every = [*self.EVERY, held, missing]
        for asked in ("all", "estate/all"):
            with self.subTest(asked=asked):
                self.assertEqual(task.pages([asked], every, records=Estate()), [*self.EVERY, held])

    def test_every_name_that_is_no_page_is_refused_together_with_the_nearest_routes(self):
        with self.assertRaises(PageRefused) as refused:
            task.pages(["estate/containers", "control_plane:containerz", "example"], self.EVERY)
        said = str(refused.exception)

        offer = f"\n  {task.estate_name(reverse('control_plane:containers'))} (the estate's control_plane:containers)"
        self.assertIn(f"No page estate/containers; type one of these instead:{offer}", said)
        self.assertIn(f"No page control_plane:containerz; type one of these instead:{offer}", said)
        self.assertIn("No page example", said)

    def test_an_estate_page_keeps_its_query_and_its_path_its_slash(self):
        self.assertEqual(task.estate_path("estate/audit/?awaiting=1"), "/audit/?awaiting=1")
        self.assertEqual(task.estate_path("estate/audit?awaiting=1"), "/audit/?awaiting=1")
        self.assertEqual(task.estate_path("estate/audit"), "/audit/")
        self.assertEqual(task.estate_path("estate"), "/")

    def test_a_page_of_any_kind_keeps_its_query(self):
        # A path copied from the address bar may come without its leading slash.
        containers = reverse("control_plane:containers")
        asked = ["control_plane:containers?q=hq", f"{containers.strip('/')}/?q=docker", f"{containers}?q=x"]

        self.assertEqual(
            task.pages(asked, self.EVERY), [f"{containers}?q=hq", f"{containers}?q=docker", f"{containers}?q=x"]
        )

    FINDINGS = ("control_plane:findings", "hq_api:findings")

    def test_a_word_is_the_one_estate_page_whose_path_it_ends(self):
        contacts, containers = reverse("contacts:list"), reverse("control_plane:containers")

        every = [*self.EVERY, containers]

        self.assertEqual(task.pages(["contacts", "containers?q=hq"], every), [contacts, f"{containers}?q=hq"])

    def test_a_word_that_ends_several_estate_pages_or_none_is_refused_with_the_nearest(self):
        # status ends a route's path, but no estate page's.
        with self.assertRaises(PageRefused) as refused:
            task.pages(["findings", "contactz", "status"], [*self.EVERY, *map(reverse, self.FINDINGS)])
        said = str(refused.exception)

        self.assertIn("No page findings; type one of these instead:", said)
        for route in self.FINDINGS:
            self.assertIn(f"\n  {task.estate_name(reverse(route))} (the estate's {route})", said)
        self.assertIn(
            "No page contactz; type one of these instead:\n  estate/contacts (the estate's contacts:list)", said
        )
        self.assertIn("No page status", said)

    def test_a_page_known_by_name_is_named_as_nearest_where_it_is_one(self):
        with self.assertRaisesMessage(PageRefused, "\n  dense/services (a page by name)"):
            task.pages(["dense/service"], self.EVERY, ["dense/services"])


class _Drawing:
    """An estate whose pages each draw the templates given them, recording what was read."""

    def __init__(self, linked, entries, unlinked, followed):
        self.drawn = {**linked, **unlinked, **followed}
        self._linked, self._entries, self._followed = list(linked), entries, list(followed)
        self.read: list[str] = []

    def linked(self):
        return self._linked

    def at(self, address):
        self.read.append(address)
        return task.Drawn(frozenset(self.drawn[address])) if address in self.drawn else None

    def entries(self):
        self.read.append("entries")
        return [(name, task.Drawn(frozenset(drawn))) for name, drawn in self._entries.items()]

    def followed(self):
        self.read.append("followed")
        return self._followed


@tag("source")
class TemplatePagesTests(SimpleTestCase):
    def estate(self):
        return _Drawing(
            linked={reverse("contacts:list"): ["contacts/contact_list.html"]},
            entries={"part/example": ["partials/_example_part.html"]},
            unlinked={reverse("calendar:entry_new"): ["calendars/_example_fields.html"]},
            followed={reverse("contacts:delete", args=[1]): ["contacts/contact_confirm_delete.html"]},
        )

    def test_a_template_reaches_the_page_or_the_catalog_entry_that_draws_it(self):
        cases = {
            "contacts/contact_list.html": reverse("contacts:list"),
            "_example_part.html": "part/example",
            "calendars/_example_fields.html": reverse("calendar:entry_new"),
            "contacts/contact_confirm_delete.html": reverse("contacts:delete", args=[1]),
        }
        for template, page in cases.items():
            with self.subTest(template=template):
                self.assertEqual(task.pages([f"template:{template}"], [], records=self.estate()), [page])

    def test_a_template_a_linked_page_draws_reads_nothing_past_it(self):
        estate = self.estate()

        task.pages(["template:contacts/contact_list.html"], [], records=estate)

        self.assertEqual(estate.read, [reverse("contacts:list")])

    def test_a_template_nothing_draws_is_refused_with_the_nearest_any_page_draws(self):
        with self.assertRaises(PageRefused) as refused:
            task.pages(["template:_example_fieldz.html"], [], records=self.estate())

        self.assertIn("\n  template:calendars/_example_fields.html", str(refused.exception))


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout


def _repository(folder: Path) -> Path:
    """A repository of one commit, made with no identity or hook of the machine's."""

    _git("init", "--quiet", cwd=folder)
    (folder / "page.txt").write_text("example\n", encoding="utf-8")
    _commit(folder)
    return folder


def _commit(folder: Path) -> None:
    """Everything in the work tree committed."""

    _git("add", "-A", cwd=folder)
    _git(
        "-c", "user.name=Example", "-c", "user.email=example@example.com", "-c", "commit.gpgsign=false",
        "commit", "--quiet", "--no-verify", "-m", "example", cwd=folder,
    )  # fmt: skip


@tag("source", "tree")
class ChangedTestsTests(SimpleTestCase):
    def test_a_test_file_whose_name_git_quotes_in_a_list_of_lines_is_named_as_it_is(self):
        """systemd's escaping writes a backslash into a unit's name, which git quotes unless asked for -z."""

        added, new, gone = (rf"tests/run\x2d{name}.mount" for name in ("added", "new", "gone"))
        with tempfile.TemporaryDirectory() as folder:
            checkout = _repository(Path(folder))
            (checkout / "tests").mkdir()
            (checkout / gone).write_text("[Mount]\n")
            _commit(checkout)
            base = task.commit_of("HEAD", checkout)
            (checkout / gone).unlink()
            (checkout / added).write_text("[Mount]\n")
            _commit(checkout)
            (checkout / new).write_text("[Mount]\n")
            self.assertEqual(task.changed_tests(base, checkout), (sorted([added, new]), [gone]))


@tag("source")
class WorktreeTests(SimpleTestCase):
    def test_the_worktree_and_its_folder_are_gone_however_the_block_ends(self):
        with tempfile.TemporaryDirectory() as folder:
            checkout = _repository(Path(folder))
            trees = []

            def broken_run():
                with task.worktree("HEAD", checkout) as tree:
                    trees.append(tree)
                    self.assertTrue((tree / "page.txt").is_file())
                    raise KeyError("the run broke")

            self.assertRaises(KeyError, broken_run)
            [tree] = trees
            self.assertFalse(tree.parent.exists())
            self.assertNotIn(str(tree), _git("worktree", "list", "--porcelain", cwd=checkout))

    def test_a_tree_asked_for_its_lint_tools_has_the_ones_it_pins_installed(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "repo").mkdir()
            checkout = _repository(Path(folder, "repo"))
            (checkout / task.LINT_TOOLS_LOCK).parent.mkdir(parents=True)
            (checkout / task.LINT_TOOLS_LOCK).write_text("{}\n", encoding="utf-8")
            _commit(checkout)
            # A stand-in for mise that installs as setup:lint-tools does, in the tree -C names.
            bin_dir = Path(folder, "bin")
            bin_dir.mkdir()
            fake = bin_dir / "mise"
            fake.write_text(
                '#!/bin/sh\n[ "$1" = -C ] && [ "$4" = setup:lint-tools ] || exit 9\n'
                'mkdir -p "$2/tools/lint/node_modules/axe-core" && : > "$2/tools/lint/node_modules/axe-core/axe.min.js"\n',
                encoding="utf-8",
            )
            fake.chmod(0o755)
            with mock.patch.dict("os.environ", {"PATH": f"{bin_dir}:/usr/bin:/bin"}):
                with task.worktree("HEAD", checkout, lint_tools=True) as tree:
                    self.assertTrue((tree / "tools/lint/node_modules/axe-core/axe.min.js").is_file())
                with task.worktree("HEAD", checkout) as tree:
                    self.assertFalse((tree / "tools/lint/node_modules").exists())

    def test_a_ref_that_names_nothing_is_said_and_leaves_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            checkout = _repository(Path(folder))
            with self.assertRaisesMessage(RefError, "example-missing"):
                task.commit_of("example-missing", checkout)
            with self.assertRaises(RefError), task.worktree("example-missing", checkout):
                self.fail("a worktree of nothing")
            self.assertEqual(_git("worktree", "list", "--porcelain", cwd=checkout).count("worktree "), 1)

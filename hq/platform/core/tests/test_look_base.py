"""dev:look against a base: the base draws in a worktree of its own code with
only this tree's probes laid over it, shoots into this run's folder, and a
page it did not draw is judged here alone and named as the look closes."""

import io
import os
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, override_settings, tag

from . import look
from .test_browser_look import ASKED, _git, _repository


@tag("source", "tree")
class WorktreeTests(SimpleTestCase):
    def test_a_report_names_the_commit_it_was_looked_at(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(BASE_DIR=folder):
            checkout = _repository(Path(folder))
            head = _git("rev-parse", "--short", "HEAD", cwd=checkout).strip()
            self.assertEqual(look._commit("HEAD"), head)
            (checkout / "page.txt").write_text("changed\n", encoding="utf-8")
            self.assertEqual(look._commit("HEAD"), f"{head} with uncommitted changes")
            self.assertIn("unknown", look._commit("example-missing"))

    def test_the_base_draws_with_its_own_test_files_judged_by_this_trees_probes(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(BASE_DIR=folder):
            checkout = _repository(Path(folder))
            tests, probes = Path(checkout, "hq", "tests"), Path(checkout, "hq", "tests", "probes")
            probes.mkdir(parents=True)
            for path in (tests / "catalog.py", tests / "fixtures.py", probes / "check.js", probes / "gone.js"):
                path.write_text("base\n", encoding="utf-8")
            _git("add", "--all", cwd=checkout)
            _git(
                "-c", "user.name=Example", "-c", "user.email=example@example.com", "-c", "commit.gpgsign=false",
                "commit", "--quiet", "--no-verify", "-m", "base", cwd=checkout,
            )  # fmt: skip
            # This tree's catalog and fixtures name what the base lacks, a test
            # file is new, a probe changed and another is gone.
            (tests / "catalog.py").write_text("here\n", encoding="utf-8")
            (tests / "fixtures.py").unlink()
            (tests / "test_new.py").write_text("new\n", encoding="utf-8")
            (probes / "check.js").write_text("here\n", encoding="utf-8")
            (probes / "gone.js").unlink()
            seen = {}

            def relay(_command, _sink, _ref, cwd, env):
                drawn = Path(cwd, "hq", "tests")
                seen["catalog"] = (drawn / "catalog.py").read_text(encoding="utf-8")
                seen["fixtures"] = (drawn / "fixtures.py").is_file()
                seen["new"] = (drawn / "test_new.py").exists()
                seen["probe"] = (drawn / "probes" / "check.js").read_text(encoding="utf-8")
                seen["gone"] = (drawn / "probes" / "gone.js").exists()
                seen["database"] = env.get("SEVERINO_TEST_DATABASE_PATH")
                seen["css"] = env.get("usage_css")
                Path(env["usage_found_into"]).write_text("[]", encoding="utf-8")

            with (
                mock.patch.dict(
                    os.environ,
                    {**ASKED, "usage_css": "main { color: red }", "SEVERINO_TEST_DATABASE_PATH": "var/example.sqlite3"},
                    clear=True,
                ),
                mock.patch.object(look, "LENS", (probes.resolve(),)),
                mock.patch.object(look, "this_run", lambda: checkout / "var" / "look" / "example-run"),
                mock.patch.object(look, "_relay", relay),
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                self.assertEqual(look._look_at("HEAD", "survey", 0.0), ({}, {}, {}, []))
            self.assertEqual(seen["catalog"], "base\n", "the base draws its own catalog")
            self.assertTrue(seen["fixtures"], "and its own fixtures")
            self.assertFalse(seen["new"], "and no test file only this tree has")
            self.assertEqual(seen["probe"], "here\n", "judged by this tree's probes")
            self.assertFalse(seen["gone"], "and none this tree took out")
            self.assertIsNone(seen["database"], "the base migrates a database of its own")
            self.assertEqual(seen["css"], "", "the base is drawn as its tree draws, without --css")

    def test_the_base_shoots_into_this_runs_folder_so_its_shots_outlive_its_worktree(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(BASE_DIR=folder):
            checkout = _repository(Path(folder))
            run = checkout / "var" / "look" / "example-run"
            run.mkdir(parents=True)

            def relay(_command, _sink, _ref, cwd, env):
                self.assertFalse(Path(env["usage_shots_into"]).is_relative_to(cwd))
                Path(env["usage_shots_into"]).mkdir()
                Path(env["usage_shots_into"], "example-402.png").write_bytes(b"base")
                Path(env["usage_found_into"]).write_text("[]", encoding="utf-8")

            with (
                mock.patch.dict(os.environ, ASKED, clear=True),
                mock.patch.object(look, "LENS", ()),
                mock.patch.object(look, "this_run", lambda: run),
                mock.patch.object(look, "_relay", relay),
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                look._look_at("HEAD", "survey", 0.0)
            self.assertEqual((run / look.BASE_OUT / "example-402.png").read_bytes(), b"base")

    def test_a_base_that_names_nothing_is_refused(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(BASE_DIR=folder):
            _repository(Path(folder))
            with (
                mock.patch.dict(os.environ, ASKED, clear=True),
                self.assertRaisesMessage(AssertionError, "--base example-missing"),
            ):
                look._look_at("example-missing", "survey", 0.0)


class _AgainstABase:
    """A survey against a base that drew one of its two pages, the other an
    entry only this tree has."""

    close = look.Look.close
    shooting = True

    def __init__(self):
        self.base = {("part/example", 402): ["overflow: a box past the edge"]}
        self.base_errors, self.base_kept = {("part/example", 402): []}, []
        self.errors = {("part/example", 402): [], ("part/example-new", 402): []}
        self.unanswered_pages = {}
        self.judged_at = {("part/example", 402): None, ("part/example-new", 402): None}
        self.tree = "example-tree"

    def tell_the_gate(self, *_args):
        pass


@tag("source")
class DrawnOnlyHereTests(SimpleTestCase):
    def test_an_entry_the_base_did_not_draw_is_judged_here_alone_and_named_as_it_closes(self):
        found = {
            ("part/example", 402): ["overflow: a box past the edge"],
            ("part/example-new", 402): ["overlap: two names"],
        }
        with (
            tempfile.TemporaryDirectory() as folder,
            override_settings(BASE_DIR=_repository(Path(folder))),
            mock.patch.object(look, "this_run", lambda: Path(folder, "var", "look", "example-run")),
            mock.patch.object(look, "tree_of", lambda _root: "example-tree"),
            mock.patch.dict(os.environ, {**ASKED, "usage_base": "HEAD"}, clear=True),
            mock.patch("sys.stdout", new_callable=io.StringIO) as said,
        ):
            Path(folder, "var", "look", "example-run").mkdir(parents=True)
            failed = _AgainstABase().close("survey", found)
        lines = said.getvalue().splitlines()
        self.assertIn("part/example-new: not drawn at HEAD, so judged here alone", lines)
        self.assertFalse(any(line.startswith("part/example:") for line in lines), "a page the base drew is compared")
        # Every finding on it is new, so it fails the run as one would without a base.
        self.assertIn("new: part/example-new at 402: overlap: two names", lines)
        self.assertTrue(failed)

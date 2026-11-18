"""setup:extensions names the set Compose would build from the same checkouts."""

import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from hq.platform.core.tests.task import trusted_mise
from tools.dev.extension_set import extension_set

MANIFEST = (
    'plugin = PluginManifest(id="example.{name}", distribution="example-{name}", django_apps=("example_{name}",))\n'
)


class ExtensionSetTests(SimpleTestCase):
    def checkout(self) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    def package(self, root: Path, name: str) -> Path:
        (root / "src" / f"example_{name}").mkdir(parents=True)
        (root / "pyproject.toml").write_text(f'[project]\nname = "example-{name}"\n')
        (root / "src" / f"example_{name}" / "plugin.py").write_text(MANIFEST.format(name=name))
        return root

    def test_every_package_of_a_repository_of_them_is_named(self):
        repository = self.checkout()
        (repository / "pyproject.toml").write_text("[tool.uv.workspace]\n")
        beta = self.package(repository / "packages" / "beta", "beta")
        alpha = self.package(repository / "packages" / "alpha", "alpha")
        single = self.package(self.checkout(), "gamma")
        named = extension_set([repository, single])
        self.assertEqual(
            named["HQ_LOCAL_PLUGINS"],
            "example_alpha.plugin:plugin,example_beta.plugin:plugin,example_gamma.plugin:plugin",
        )
        for root in (alpha, beta, single):
            self.assertIn(str(root.resolve() / "src"), named["HQ_LOCAL_PYTHONPATH"])

    def test_a_checkout_with_no_package_is_refused(self):
        with self.assertRaises(SystemExit):
            extension_set([self.checkout()])

    def test_a_package_whose_manifest_disagrees_with_it_is_refused(self):
        root = self.package(self.checkout(), "alpha")
        (root / "pyproject.toml").write_text('[project]\nname = "example-other"\n')
        with self.assertRaises(SystemExit):
            extension_set([root])

    def test_a_value_the_environment_holds_wins_over_the_file_and_the_file_names_the_rest(self):
        from tools.dev.extension_set import write

        project = self.checkout()
        empty = project / "global.toml"
        empty.write_text("")
        written = {"HQ_LOCAL_PYTHONPATH": "/srv/example/src", "HQ_LOCAL_PLUGINS": "example_alpha.plugin:plugin"}
        isolated = {
            **os.environ,
            **trusted_mise(project, project / "state"),
            "MISE_CEILING_PATHS": str(project.parent),
            "MISE_GLOBAL_CONFIG_FILE": str(empty),
            "MISE_SYSTEM_CONFIG_FILE": str(empty),
        }
        isolated.pop("HQ_LOCAL_PYTHONPATH", None)
        isolated.pop("HQ_LOCAL_PLUGINS", None)
        with mock.patch.dict(os.environ, isolated, clear=True):
            write(project / "mise.local.toml", written)

        def resolved(**given: str) -> dict[str, str]:
            done = subprocess.run(
                ["mise", "-C", str(project), "env", "--json"],
                env={**isolated, **given},
                capture_output=True,
                text=True,
                check=True,
            )
            return {name: json.loads(done.stdout)[name] for name in written}

        self.assertEqual(resolved(), written)
        chosen = "example_beta.plugin:plugin"
        self.assertEqual(resolved(HQ_LOCAL_PLUGINS=chosen), {**written, "HQ_LOCAL_PLUGINS": chosen})


class CoordinatedBranchTests(SimpleTestCase):
    """A host tree landing on a branch its extension repository also carries composes that branch, as CI does."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.work = Path(directory.name).resolve()
        snapshots = mock.patch("tools.dev.extension_set.SNAPSHOTS", self.work / "snapshots")
        snapshots.start()
        self.addCleanup(snapshots.stop)
        # The host speaks API 2 on main and on pass/x; the tree asked is a worker's branch made from pass/x.
        self.host = self.work / "host"
        (self.host / "hq/platform/application").mkdir(parents=True)
        self.git(self.host, "init", "-q", "-b", "main")
        self.commit(self.host, "hq/platform/application/plugins.py", "PLUGIN_API_VERSION = 2\n")
        self.git(self.host, "switch", "-q", "-c", "pass/x")
        self.commit(self.host, "notes.txt", "coordinated\n")
        self.git(self.host, "switch", "-q", "-c", "worker")
        # The checkout named stands on main at API 1, and no commit of its history declares API 2.
        self.extensions = self.work / "extensions"
        self.extensions.mkdir()
        self.git(self.extensions, "init", "-q", "-b", "main")
        self.commit(self.extensions, "packages/alpha/pyproject.toml", '[project]\nname = "example-alpha"\n')
        self.declare(self.extensions, 1, "main")
        self.git(self.extensions, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.git(self.extensions, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.git(self.extensions, "branch", "pass/x")
        self.coordinated = self.work / "coordinated"
        self.git(self.extensions, "worktree", "add", "-q", str(self.coordinated), "pass/x")
        self.declare(self.coordinated, 2, "branch")
        self.environ = {
            "HQ_LOCAL_PLUGINS": "example_alpha.plugin:plugin",
            "HQ_LOCAL_PYTHONPATH": str(self.extensions / "packages/alpha/src"),
        }

    @staticmethod
    def git(root: Path, *args: str) -> str:
        isolated = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        done = subprocess.run(["git", "-C", str(root), *args], env=isolated, capture_output=True, text=True, check=True)
        return done.stdout.strip()

    def commit(self, root: Path, path: str, text: str) -> None:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
        self.git(root, "add", "-A")
        self.git(root, "commit", "-q", "--no-verify", "-m", path)

    def declare(self, root: Path, api: int, word: str) -> None:
        self.commit(
            root,
            "packages/alpha/src/example_alpha/plugin.py",
            f'# {word}\nplugin = PluginManifest(id="example.alpha", api_version={api})\n',
        )

    def picked(self):
        from tools.dev.extension_set import for_tree

        return for_tree(self.host, environ=self.environ)

    def imported(self, source: str) -> str:
        return (Path(source) / "example_alpha" / "plugin.py").read_text()

    def test_the_worktree_on_the_branch_is_composed_as_it_stands(self):
        manifest = self.coordinated / "packages/alpha/src/example_alpha/plugin.py"
        manifest.write_text(f"# uncommitted\n{manifest.read_text()}")
        picked = self.picked()
        self.assertEqual(picked.sources, (str(self.coordinated / "packages/alpha/src"),))
        self.assertIn("# uncommitted", self.imported(picked.sources[0]))
        self.assertIn("pass/x", picked.said)
        self.assertEqual(picked.stood, ((str(self.extensions), str(self.coordinated)),))

    def test_a_branch_no_worktree_holds_is_composed_at_its_commit(self):
        self.git(self.extensions, "worktree", "remove", "--force", str(self.coordinated))
        picked = self.picked()
        self.assertEqual(len(picked.sources), 1)
        self.assertIn("# branch", self.imported(picked.sources[0]))
        self.assertIn("pass/x", picked.said)
        self.assertEqual(picked.at, ((str(self.extensions), self.git(self.extensions, "rev-parse", "pass/x")),))

    def test_a_worktree_named_on_the_command_line_is_composed_as_it_stands(self):
        chosen = self.work / "chosen"
        self.git(self.extensions, "worktree", "add", "-q", "-b", "pass/y", str(chosen), "main")
        self.declare(chosen, 2, "chosen")
        self.environ["HQ_LOCAL_PYTHONPATH"] = str(chosen / "packages/alpha/src")
        picked = self.picked()
        self.assertEqual(picked.sources, (str(chosen / "packages/alpha/src"),))
        self.assertNotIn("pass/x", picked.said)

    def test_a_tree_landing_on_the_default_branch_or_detached_keeps_the_named_set(self):
        for switch in (("-c", "from-main", "main"), ("--detach", "pass/x")):
            with self.subTest(switch=switch):
                self.git(self.host, "switch", "-q", *switch)
                picked = self.picked()
                self.assertNotIn("pass/x", picked.said)
                self.assertFalse(any(source.startswith(str(self.coordinated)) for source in picked.sources))

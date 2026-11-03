"""image:verify:tree holds the composed suite's tree to the image's /app, whatever form a dockerignore line or the COPY takes."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, tag

ROOT = Path(__file__).resolve().parents[4]
CHECK = "mise-tasks/image/verify/tree"
LIBRARIES = ("export-tree.sh", "image-tree.sh")

# A checkout's files, and what the image built from it holds in /app with
# this repository's Dockerfile and Dockerfile.dockerignore: docs/ is not
# admitted, controller/ and mise.toml are other stages' inputs, and the
# controller's host programs and the bytecode beside a module are built.
CHECKOUT = ("hq/app.py", "hq/README.md", "deploy/unit.sh", "docs/guide.md", "controller/main.go", "mise.toml")
SHIPPED = (
    "hq/app.py",
    "hq/README.md",
    "deploy/unit.sh",
    "deploy/bin/hq-host",
    "deploy/bin/hq-secrets",
    "hq/__pycache__/app.cpython-315.pyc",
)

# The image's /app, printed by a docker that answers every call with it.
FAKE_DOCKER = '#!/bin/sh\ncat "${FAKE_APP_LISTING:?}"\n'


@tag("source")
class ImageTreeTests(SimpleTestCase):
    def check(self, listing: tuple[str, ...], dockerfile: str | None = None) -> subprocess.CompletedProcess[str]:
        """The check, run in a copy of this checkout's build inputs, against an image whose /app lists ``listing``."""

        with tempfile.TemporaryDirectory() as folder:
            checkout = Path(folder) / "checkout"
            for name in CHECKOUT:
                (checkout / name).parent.mkdir(parents=True, exist_ok=True)
                (checkout / name).write_text(name)
            (checkout / "Dockerfile").write_text(dockerfile or (ROOT / "Dockerfile").read_text())
            shutil.copy(ROOT / "Dockerfile.dockerignore", checkout)
            (checkout / "tools/lib").mkdir(parents=True)
            for library in LIBRARIES:
                shutil.copy(ROOT / "tools/lib" / library, checkout / "tools/lib")
            subprocess.run(["git", "init", "--quiet", str(checkout)], check=True)
            bin_dir = Path(folder) / "bin"
            bin_dir.mkdir()
            (bin_dir / "docker").write_text(FAKE_DOCKER)
            (bin_dir / "docker").chmod(0o755)
            (Path(folder) / "listing").write_text("".join(f"{path}\n" for path in listing))
            environment = {
                **os.environ,
                "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                "FAKE_APP_LISTING": str(Path(folder) / "listing"),
                "TMPDIR": folder,
            }
            return subprocess.run(
                ["/bin/sh", str(ROOT / CHECK)],
                check=False,
                cwd=checkout,
                env=environment,
                capture_output=True,
                text=True,
            )

    def test_the_image_holding_the_suites_tree_passes(self) -> None:
        result = self.check(SHIPPED)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_path_only_the_image_holds_is_named(self) -> None:
        # What a `!docs/**/*.md` re-admission ships, which the suite's reading of the dockerignore drops.
        result = self.check((*SHIPPED, "docs/guide.md"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("docs/guide.md", result.stderr)

    def test_a_path_only_the_suite_holds_is_named(self) -> None:
        # What an exclusion after an admission leaves out of the image.
        result = self.check(tuple(path for path in SHIPPED if path != "hq/README.md"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("hq/README.md", result.stderr)

    def test_copy_flags_the_suite_cannot_read_fail(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text()
        copy = next(line for line in dockerfile.splitlines() if line.startswith("COPY --exclude="))
        split = copy.replace(" --exclude=", " \\\n    --exclude=")
        result = self.check(SHIPPED, dockerfile.replace(copy, split))
        self.assertEqual(result.returncode, 1)
        self.assertIn("controller/main.go", result.stderr)
        self.assertIn("mise.toml", result.stderr)

    def test_only_what_a_stage_builds_is_left_out(self) -> None:
        # Bytecode beside no module of the tree is no compileall output.
        result = self.check((*SHIPPED, "hq/__pycache__/gone.cpython-315.pyc"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("hq/__pycache__/gone.cpython-315.pyc", result.stderr)

    def test_a_built_program_the_image_lacks_is_named(self) -> None:
        result = self.check(tuple(path for path in SHIPPED if path != "deploy/bin/hq-host"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("deploy/bin/hq-host", result.stderr)

"""audit:churn counts each tracked file's commits, leaving out what .gitattributes marks generated or vendored."""

import os
import re
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, tag

from tests.mise_tasks import ROOT

SCRIPT = ROOT / "mise-tasks" / "audit" / "churn"
IDENTITY = {
    "GIT_AUTHOR_NAME": "Example",
    "GIT_AUTHOR_EMAIL": "example@example.com",
    "GIT_COMMITTER_NAME": "Example",
    "GIT_COMMITTER_EMAIL": "example@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


@tag("source")
class AuditChurnListingTests(SimpleTestCase):
    def churn(self, root: Path, since: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/sh", str(SCRIPT)],
            cwd=root,
            env=os.environ | IDENTITY | {"usage_since": since},
            capture_output=True,
            text=True,
            check=False,
        )

    def test_each_source_files_commits_are_counted_most_first(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            env = os.environ | IDENTITY

            def commit(files: dict[str, str | None]) -> None:
                for name, text in files.items():
                    path = root / name
                    if text is None:
                        path.unlink()
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(text)
                subprocess.run(["git", "add", "-A"], cwd=root, env=env, check=True)
                subprocess.run(
                    ["git", "-c", "commit.gpgsign=false", "commit", "--no-verify", "-q", "-m", "change"],
                    cwd=root,
                    env=env,
                    check=True,
                )

            subprocess.run(["git", "init", "-q"], cwd=root, env=env, check=True)
            commit(
                {
                    ".gitattributes": "made.json linguist-generated\nvendor/** linguist-vendored\n",
                    "a.py": "1",
                    "gone.py": "1",
                    "made.json": "1",
                    "vendor/lib.js": "1",
                }
            )
            commit({"a.py": "2", "b.py": "1", "made.json": "2", "vendor/lib.js": "2", "gone.py": None})
            commit({"a.py": "3"})
            run = self.churn(root, "2000-01-01")

        self.assertEqual(run.returncode, 0, run.stderr)
        *listed, closing = run.stdout.splitlines()
        self.assertEqual(listed, ["3\ta.py", "1\t.gitattributes", "1\tb.py"])
        self.assertRegex(closing, r"^3 files")


# A sort command in a shell line, never jq's sort_by or Python's sorted.
SORT = re.compile(r"(?<![\w.=-])(?:LC_ALL=C )?sort(?= |$)")


@tag("source")
class AuditListingsSortByBytesTests(SimpleTestCase):
    """An audit's listing is compared by a diff, so its order is the same under every locale."""

    def test_every_sort_an_audit_script_runs_is_by_bytes(self):
        scripts = [
            path for path in (ROOT / "mise-tasks" / "audit").iterdir() if path.read_text().startswith("#!/bin/sh")
        ]
        self.assertTrue(scripts)
        for script in scripts:
            for number, line in enumerate(script.read_text().splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                for found in SORT.finditer(line):
                    with self.subTest(script=script.name, line=number):
                        self.assertTrue(found.group().startswith("LC_ALL=C "), line)

"""The files .gitattributes marks linguist-generated are the ones the tasks declare they write.

audit:churn and GitHub's diff read the attribute, so a declared file the
attribute misses is counted as hand-written change, and a marked file nothing
declares is hidden from both. lint:drift-gitattributes holds the file's text to
the declarations; this holds what git reads in it.
"""

import subprocess

from django.test import SimpleTestCase, tag

from hq.platform.application.tests.reads import reads
from tests.mise_tasks import ROOT, mise_tasks, writes


def tracked(*pathspec: str) -> set[str]:
    listed = subprocess.run(["git", "ls-files", "-z", "--", *pathspec], cwd=ROOT, capture_output=True, check=True)
    return {name.decode() for name in listed.stdout.split(b"\0") if name}


@tag("source", "tree")
@reads(
    "mise*.toml",
    "mise.lock",
    "mise-tasks/**",
    "controller/go.mod",
    "tools/pipeline/toolchain.env",
    "**/.gitattributes",
    lists=("**",),
)
class GeneratedAttributeTests(SimpleTestCase):
    def test_the_attribute_marks_exactly_what_the_tasks_declare(self) -> None:
        declared = {path: tracked(f":(glob){path}") for task in mise_tasks().values() for path in writes(task)}
        self.assertTrue(declared, "no task declares a file it writes")
        for path, files in declared.items():
            with self.subTest(path):
                self.assertTrue(files, f"{path} is declared written, and git tracks no file it names")
        self.assertEqual(tracked(":(attr:linguist-generated)"), set().union(*declared.values()))

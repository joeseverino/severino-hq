"""The files .gitattributes marks linguist-generated are the ones `mise run fix` writes.

audit:churn and GitHub's diff read the attribute, and the derived-file gates
hold the writers, so a file one writes that the attribute misses is counted
as hand-written change, and a marked file nothing writes is hidden from both.
"""

import subprocess

from django.test import SimpleTestCase, tag

from hq.domains.control_plane import bridge_contract
from hq.domains.control_plane.management.commands.connections_reference import reference_path
from hq.platform.api.openapi import DOCUMENT_PATH, EXAMPLES_PATH
from hq.platform.application.procedure_schema import SCHEMA_PATH
from hq.platform.application.tests.reads import reads
from hq_sdk.contract import CONTRACT_PATH
from hq_sdk.text_contract import EXAMPLES_PATH as TEXT_EXAMPLES_PATH
from tests.mise_tasks import ROOT
from tools.fonts.subset import SERVED


def tracked(*pathspec: str) -> set[str]:
    listed = subprocess.run(["git", "ls-files", "-z", "--", *pathspec], cwd=ROOT, capture_output=True, check=True)
    return {name.decode() for name in listed.stdout.split(b"\0") if name}


@tag("source", "tree")
@reads(".gitattributes", "hq/domains/control_plane/bridge_base.json", lists=("**",))
class GeneratedAttributeTests(SimpleTestCase):
    def test_the_attribute_marks_exactly_what_the_writers_write(self) -> None:
        written = {
            CONTRACT_PATH,
            TEXT_EXAMPLES_PATH,
            DOCUMENT_PATH,
            EXAMPLES_PATH,
            reference_path(),
            SCHEMA_PATH,
            SERVED,
            *bridge_contract.emitted(),
        }
        # go generate's outputs, which lint:controller holds to their specs.
        expected = {str(path.relative_to(ROOT)) for path in written} | tracked("controller/*.gen.go")
        self.assertEqual(tracked(":(attr:linguist-generated)"), expected)

"""Each piece of evidence an extension's admission claims, held to the step that produces it."""

import json
import re

from django.test import SimpleTestCase, tag

from .test_pipeline import ADMIT, ROOT, every_step, load, script


@tag("source")
class AdmissionEvidenceTests(SimpleTestCase):
    """The admission's evidence. It reads YAML, so it stays out of
    test_plugins, which an extension's plugin check runs without PyYAML."""

    def test_every_evidence_id_the_policy_requires_has_a_step_that_produces_it(self):
        """Cordon's evidence step marks every id the policy lists as passed.

        So an id is true only because the step producing it ran and passed
        first: in the admission action, before the step that signs, or in
        `plugin-checks.yml`, the job an extension's admission needs. Each id
        names its producer here, by job and step id.
        """

        action = load(ROOT / ".github" / "actions" / "admit-plugin" / "action.yml")["runs"]["steps"]
        checks = load(ROOT / ".github" / "workflows" / "plugin-checks.yml")["jobs"]
        produced_by = {
            "plugin-contract-tests": ("test", "plugin-checks"),
            "package-tests": ("test", "plugin-checks"),
            "secret-scan": ("secret-scan", None),
            "dependency-lock": (None, "dependency-lock"),
            "dependency-audit": (None, "dependency-audit"),
            "wheel-sbom": (None, "wheel-sbom"),
        }
        policy = json.loads((ROOT / "policy" / "plugin-admission-v1.json").read_text())
        self.assertEqual(sorted(policy["required_evidence"]), sorted(produced_by))
        signs = next(index for index, step in enumerate(action) if "cordon/admission/evidence" in step.get("run", ""))
        for evidence, (job, step_id) in produced_by.items():
            with self.subTest(evidence=evidence):
                if job is None:
                    ids = [step.get("id") for step in action]
                    self.assertIn(step_id, ids, "the admission action has no step of this id")
                    self.assertLess(ids.index(step_id), signs, "the step runs after the evidence is signed")
                    self.assertNotIn("if", action[ids.index(step_id)], "a skipped step produces nothing")
                    continue
                self.assertIn(job, checks, "plugin-checks.yml has no job of this name")
                if step_id is not None:
                    self.assertIn(step_id, [step.get("id") for step in checks[job]["steps"]])

    def test_the_sbom_an_admission_writes_is_the_one_it_signs_and_retains(self):
        steps = every_step(ADMIT)
        (written,) = [
            re.search(r"--output-file (\S+)", script(step)) for step in steps if step.get("id") == "wheel-sbom"
        ]
        assert written is not None, "the SBOM step names no output file"
        sbom = written.group(1)
        self.assertIn(f"--sbom {sbom}", "\n".join(script(step) for step in steps))
        (retained,) = [step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@")]
        self.assertIn(sbom, retained["with"]["path"].split())

"""Where the pipeline's scanners come from: Trivy and gitleaks run as tasks from mise.lock."""

import re

from django.test import SimpleTestCase, tag

from hq.platform.application.tests.test_pipeline import (
    ROOT,
    code,
    composite_actions,
    jobs,
    load,
    script,
    steps,
    uses,
    workflow,
    workflows,
)


@tag("source")
class ScannerTests(SimpleTestCase):
    """A scanner runs from mise.lock, as a task, never from a third party's action."""

    def test_trivy_and_gitleaks_run_from_the_lock_never_their_vendors_action(self):
        for path in [*workflows(), *composite_actions()]:
            for action in uses(load(path)):
                with self.subTest(file=path.name, action=action):
                    self.assertFalse(action.startswith(("aquasecurity/", "gitleaks/")))

    def test_the_extensions_secret_scan_is_the_hosts_task_at_the_workflows_own_commit(self):
        """An extension calls plugin-checks.yml at the host's main, so the scan
        it runs is the task beside that workflow, with gitleaks from that
        commit's mise.lock."""
        scan = jobs(workflow("plugin-checks.yml"))["secret-scan"]
        (host,) = [step for step in steps(scan) if step.get("with", {}).get("path") == "host"]
        self.assertEqual(host["with"]["ref"], "${{ job.workflow_sha }}")
        self.assertEqual(host["with"]["repository"], "${{ job.workflow_repository }}")
        (tool,) = [step for step in steps(scan) if step.get("uses") == "./host/.github/actions/host-tool"]
        self.assertEqual(tool["with"]["tool"], "gitleaks")
        (run,) = [step for step in steps(scan) if "scan:secrets" in script(step)]
        self.assertIn('mise -C host run scan:secrets "$GITHUB_WORKSPACE"', script(run))

    def test_composes_scan_is_the_task_and_the_attested_sbom_is_the_file_it_scanned(self):
        """Compose scans through image:trivy, which runs the Trivy mise.lock
        checksums and writes each report to a file rather than the package
        table. The release attests the SBOM the vulnerability gate scanned,
        and secrets in the image are a pass of their own."""
        compose_steps = [step for job in jobs(workflow("compose.yml")).values() for step in steps(job)]
        (scan,) = [step for step in compose_steps if "mise run image:trivy" in script(step)]
        (written,) = re.findall(r"--sbom (\S+)", script(scan))
        (attest,) = [step for step in compose_steps if "cosign attest" in script(step)]
        self.assertIn(f"--predicate {written} ", script(attest))
        self.assertNotIn("trivy ", script(attest))
        # One command per line, its continuation lines joined to it.
        commands = re.sub(r"\\\n\s*", " ", code((ROOT / "mise-tasks" / "image" / "trivy").read_text())).splitlines()
        (sbom,) = [re.search(r"--output (\S+)", line)[1] for line in commands if "--format cyclonedx" in line]
        (gate,) = [line.split() for line in commands if line.lstrip().startswith("trivy sbom ")]
        self.assertIn("--exit-code", gate)
        # What it scans, its last argument before `||`, is the SBOM written.
        self.assertEqual(gate[gate.index("||") - 1], sbom)
        (secrets,) = [line for line in commands if "--scanners secret" in line]
        self.assertIn("--exit-code 1", secrets)

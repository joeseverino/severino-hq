"""A playbook's command, as an agent's catalog describes it, says what the
adopted catalog declares the run checks before it works, how it shows it
worked, and how a failed one is recovered."""

from django.test import TestCase

from ..capabilities import describe_capabilities
from ..security import Capability, Principal
from .procedures import RESTART_RUN, Supply, manifest

AGENT = Principal("example-agent", "mcp", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
LOGS = "example.service_logs"


def notes(name):
    (described,) = describe_capabilities(principal=AGENT, name=name)["capabilities"]
    return described["execution_notes"]


class ReviewedNotesTests(TestCase):
    def setUp(self):
        self.supply = Supply(
            self,
            manifest(
                preconditions=("The service is installed", "Its unit file is present."),
                verification=("The service answers its health check.",),
                recovery=("Start the service by hand if it stays down.",),
            ),
            manifest(id=LOGS, effect="read", retry="safe", preconditions=(), verification=()),
        )

    def test_a_change_says_its_checks_its_proof_and_its_recovery(self):
        said = " ".join(notes(RESTART_RUN))

        for statement in (
            "The service is installed.",
            "Its unit file is present.",
            "The service answers its health check.",
            "Start the service by hand if it stays down.",
        ):
            self.assertIn(statement, said)

    def test_a_read_that_states_no_checks_says_its_recovery_and_no_empty_step(self):
        said = notes(f"automation.{LOGS}.run")

        self.assertIn("Start the service by hand if it stays down.", " ".join(said))
        # A step with nothing after its lead would be a check the catalog never declared.
        self.assertFalse([note for note in said if note.rstrip().endswith(":")], said)

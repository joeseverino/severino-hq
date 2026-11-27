"""A declaration is written only under the runner's consent, whatever writes it.

A writer takes the runner's consent rather than a principal, and only the
runner makes one, so the hold cannot be walked around by calling a writer;
and the consent judges the kind actually written, so a command that never
says it acts on a declaration cannot write a gated one past the hold.
"""

from dataclasses import replace
from types import SimpleNamespace

from django.test import TestCase

from hq.domains.control_plane.models import ManagedResource
from hq.platform.core.testing import declare

from ..approvals import ApproveCommand, approve
from ..capabilities import execute_capability
from ..infrastructure import ManagedResourceCommand
from ..security import Capability, Principal, mcp_principal
from .test_approvals import GATED_KIND, POLICY_KEY, an_operator, policy_document
from .writers import keyed

TOKEN = Principal("a-token", "mcp", frozenset({Capability.MANAGE_INFRASTRUCTURE}))


def amended() -> ManagedResourceCommand:
    return ManagedResourceCommand(key=POLICY_KEY, kind=GATED_KIND, spec={"document": policy_document("group:else")})


class ConsentTests(TestCase):
    def setUp(self):
        from hq.platform.core.testing import managing_everything

        managing_everything()
        declare(POLICY_KEY, GATED_KIND, {"document": policy_document()})

    def policy(self) -> str:
        return ManagedResource.objects.get(key=POLICY_KEY).spec["document"]

    def test_running_a_change_approved_takes_a_persons_approval_not_a_principal(self):
        import copy
        import pickle
        from unittest import mock

        from .. import approvals
        from ..capabilities import command_spec, execute_approved, validate_command

        spec = command_spec("infrastructure.resource.update")
        wanted = {"key": POLICY_KEY, "kind": GATED_KIND, "spec": amended().spec, "enabled": True}
        payload = keyed(wanted)
        command = validate_command(spec, wanted, POLICY_KEY)

        from ..approvals import Approval, Approved
        from ..sealing import sealer

        # A real one, as a person's approval hands it over, to copy and pickle.
        asked = execute_capability(
            "infrastructure.resource.update", payload, principal=mcp_principal(), target=POLICY_KEY
        )
        with mock.patch.object(approvals, "execute_approved", return_value={"ok": True}) as handed:
            approve(ApproveCommand(asked["approval"]["id"]), principal=an_operator())
        (real,) = handed.call_args.args
        fields = {
            "spec": spec,
            "command": command,
            "target": POLICY_KEY,
            "acting": replace(TOKEN, approved_by="operator"),
            "sent": frozenset(payload),
        }
        seal, _unseal = sealer(Approved, Approval)
        forged = {
            "called": Approved,
            "duck-typed": lambda: SimpleNamespace(**fields),
            "bare": lambda: object.__new__(Approved),
            "sealed elsewhere": lambda: seal(Approval(**fields)),
            "copied": lambda: copy.copy(real),
            "pickled": lambda: pickle.loads(pickle.dumps(real)),
        }
        for way, forge in forged.items():
            with self.subTest(way=way), self.assertRaises(TypeError):
                execute_approved(forge())
        self.assertEqual(self.policy(), policy_document())

        execute_approved(real)
        self.assertEqual(self.policy(), amended().spec["document"])

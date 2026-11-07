"""A refusal in the audit log ends in the words its code declares, beside the code
in ``refusal_status.CODES``, and every code a denial can be given is declared there."""

from django.test import SimpleTestCase

from .. import agent_access, agent_limits
from ..agent_devices import Unplaced
from ..approvals import RefusedHold
from ..denials import _message
from ..refusal_status import CODES
from ..security import AuthorizationError
from ..tokens import TokenError

UNDECLARED = "example_undeclared"


def _family(cls):
    """``cls`` and every subclass of it."""

    found = [cls]
    for sub in cls.__subclasses__():
        found.extend(_family(sub))
    return found


def _recordable():
    """Every code a refusal is recorded with, from the declarations that raise or return one.

    ``refusal_of`` records an authorization refusal and a hold that could not
    wait, and ``agent_admission`` a token it rejects, a brake that applies and
    an agent that asks too much.
    """

    codes = {cls.code for base in (AuthorizationError, RefusedHold, TokenError) for cls in _family(base)}
    codes |= {value.code for value in vars(agent_access).values() if isinstance(value, agent_access.AgentRefusal)}
    codes |= {str(code) for code in Unplaced}
    codes |= {agent_limits.RATE_LIMITED, agent_limits.TOO_MANY_CALLS}
    return codes


class RefusalWordsTests(SimpleTestCase):
    def test_every_code_a_denial_can_be_given_is_declared(self):
        self.assertEqual(_recordable() - set(CODES), set())

    def test_every_declared_code_ends_its_sentence_in_its_own_words(self):
        for code, facts in CODES.items():
            with self.subTest(code=code):
                message = _message(code, actor="example", capability="", detail="")

                self.assertTrue(facts.said)
                self.assertNotIn("_", facts.said, "a code's words are never the code")
                self.assertFalse(facts.said.endswith((".", ":", ";")), "the sentence ends after it")
                self.assertEqual(message, f"example was refused: {facts.said}")

    def test_an_undeclared_code_reads_as_a_plain_sentence(self):
        message = _message(UNDECLARED, actor="example", capability="", detail="")

        self.assertEqual(message, "example was refused")
        self.assertNotIn("undeclared", message)

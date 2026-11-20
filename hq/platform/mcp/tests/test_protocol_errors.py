"""A refusal outside a tool call is a protocol error of the class its HTTP status has,
so a new refusal code needs no edit at the MCP door."""

from django.test import SimpleTestCase
from mcp import types

from hq.platform.application.refusal_codes import Code
from hq.platform.application.refusal_status import Refusal

from ..server import _as_protocol_error

# What each status says, as JSON-RPC has it: the server failed, the request's
# parameters are wrong, or the request stands refused as it is.
SERVER = frozenset({500, 503, 504})
PARAMETERS = frozenset({400, 404, 405, 413, 415})
STANDING = frozenset({401, 403, 409, 429})
CLASS = {types.INTERNAL_ERROR: SERVER, types.INVALID_PARAMS: PARAMETERS, types.INVALID_REQUEST: STANDING}


def _protocol(code):
    raised = _as_protocol_error(Refusal(code, "Refused."))
    return raised.error.code, raised.error.data


class ProtocolErrorTests(SimpleTestCase):
    def test_every_refusal_code_is_the_protocol_error_of_its_status_class(self):
        for code in Code:
            with self.subTest(code=code):
                number, data = _protocol(code)
                self.assertIn(code.status, CLASS[number])
                self.assertEqual(data, {"code": code})

"""A read with no resource of its own takes its caller and refuses one that may read nothing.

So every door that answers it, MCP's tools, resources and prompts and the
host's shell, asks it the same question and none has to remember to.
"""

from django.test import TestCase

from .. import approvals, read_models, registry
from ..security import AuthorizationError, Capability, Principal

WRITER = Principal("example-writer", "mcp", frozenset({"write_projects"}))
READER = Principal("example-reader", "mcp", frozenset({Capability.READ}))

READS = {
    "audit_registry": lambda principal: registry.audit_registry(principal=principal),
    "documentation_status": lambda principal: read_models.documentation_status(principal=principal),
    "system_health": lambda principal: read_models.system_health(principal=principal),
    "held requests": lambda principal: approvals.pending(principal=principal),
}


class ReadsRefuseThemselvesTests(TestCase):
    def test_a_caller_that_may_read_nothing_is_refused_each(self):
        for name, read in READS.items():
            with self.subTest(name), self.assertRaises(AuthorizationError):
                read(WRITER)

    def test_a_reader_is_answered_each(self):
        for name, read in READS.items():
            with self.subTest(name):
                read(READER)

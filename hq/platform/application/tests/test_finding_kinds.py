"""Which kinds of resource a finding is raised for, and when what HQ knows of one has gone stale."""

from django.test import TestCase

from hq.domains.control_plane.models import ManagedResource
from hq.platform.application.plugin_testing import form_keyed
from hq.platform.core.testing import MANAGER, declare, operator

from ..findings import estate_findings
from ..report_testing import bridged
from .host_only import host_only


class UnrecognizedContainerTests(TestCase):
    """A container no compose project declares is reported, never adopted.

    The sweep leaves it unmanaged, so it has no node of its own. Its machine
    carries it as a fact, and the finding reads it from there and offers the
    one adoption that takes exactly that record on.
    """

    STRAY = {
        "name": "a-stray",
        "host": "a-docker-host",
        "connection_ref": "a-portainer",
        "stack": "",
    }
    COMPOSED = {
        "name": "a-web",
        "host": "a-docker-host",
        "connection_ref": "a-portainer",
        "stack": "a-project",
    }

    def setUp(self):
        from hq.platform.core.testing import connection

        connection("portainer", "a-portainer", manages=True)
        declare("a-docker-host", "machine", {"name": "a-docker-host", "addresses": ["10.0.0.9"]})
        bridged(
            "inventory",
            {
                "portainer.container": {
                    "ok": True,
                    "records": [self.COMPOSED, self.STRAY],
                }
            },
        )

    def found(self):
        with host_only():
            return [f for f in estate_findings(principal=MANAGER) if f.rule == "unrecognized-container"]

    def test_adopting_it_from_the_page_clears_the_finding(self):

        (finding,) = self.found()
        operator(self.client)

        self.client.post(finding.presses[0].url, form_keyed())

        self.assertTrue(ManagedResource.objects.filter(kind="portainer.container", spec__name="a-stray").exists())
        self.assertEqual(self.found(), [])

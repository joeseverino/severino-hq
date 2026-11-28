"""HQ's jobs and its automation runs are one list: filtered, ordered and paged
in the database, each row said in the words and tones a job is. Every
procedure here is a synthetic ``example.*`` one."""

from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import OperationRequest
from hq.platform.core.testing import AGENT

from ..controller import claim_next_operation
from ..resources import list_resource
from ..security import Principal, operator_capabilities
from ..work_runs import (
    NEWEST,
    hydrated,
    runs_on,
    work,
)
from .procedures import APP, RESTART, RESTART_RUN, Supply, approved_run, manifest, run_payload

LOGS = "example.service_logs"
WORKER = "example-worker"
CLAIMS = (("automation.operation", "run"),)
READER = Principal("example-reader", "test", operator_capabilities())


class WorkRunsTestCase(TestCase):
    def setUp(self):
        self.supply = Supply(self, manifest(), manifest(id=LOGS))
        self.keys = iter(range(1000))

    def run_of(self, *hosts, name=RESTART_RUN, **fields):
        answer = approved_run(
            self, run_payload(*hosts, **fields), principal=AGENT, key=f"example-{next(self.keys)}", name=name
        )
        self.assertTrue(answer["ok"], answer)
        return OperationRequest.objects.get(pk=answer["operation"]["id"])

    def claimed(self, run):
        claim = claim_next_operation(WORKER, capabilities=CLAIMS)["operation"]
        self.assertEqual(claim["id"], str(run.pk))
        run.refresh_from_db()
        return run

    def listed(self):
        return hydrated(work().kept().order_by(*NEWEST))

    def read(self, **query):
        """What the list read answers for ``query``, newest first."""

        asked = {"sort": "-created_at", "limit": 100, **query}
        return list_resource("work", asked, principal=READER, fields=["*"])["items"]


class OrderTests(WorkRunsTestCase):
    def ago(self, record, hours):
        type(record).objects.filter(pk=record.pk).update(created_at=timezone.now() - timedelta(hours=hours))
        return record

    def test_a_run_is_called_by_its_playbook_s_title_and_found_by_it(self):
        """The title the playbook was adopted with, on every list of work; a
        run of a playbook no longer adopted keeps its id."""

        restarted = self.run_of(APP)
        titles = "hq.platform.application.automation_adoption.playbook_titles"
        with mock.patch(titles, return_value={RESTART: "Restart a service"}):
            (row,) = self.listed()
            (found,) = self.read(query="restart a serv")
            (machine_row,) = runs_on(APP)
        self.assertEqual((row.id, row.title, found["id"]), (str(restarted.pk), "Restart a service", str(restarted.pk)))
        self.assertEqual(machine_row.title, "Restart a service")
        with mock.patch(titles, return_value={}):
            (row,) = self.listed()
        self.assertIn(RESTART, row.title)

    def test_a_preview_is_called_a_preview_of_its_playbook(self):
        """A run in check mode, on every list of work."""

        self.run_of(APP, check=True)
        with mock.patch(
            "hq.platform.application.automation_adoption.playbook_titles", return_value={RESTART: "Restart"}
        ):
            (row,) = self.listed()
            (machine_row,) = runs_on(APP)
        self.assertTrue(row.title.startswith("Preview"), row.title)
        self.assertIn("Restart", row.title)
        self.assertEqual(machine_row.title, row.title)

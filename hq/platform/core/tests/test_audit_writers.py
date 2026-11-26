"""A change to a row is written once: by its model's audit signal.

The calls that write one by hand, a work's end or a handler's run outside the
runner are ast-grep rules (row-change-recorded-by-its-signal,
work-end-recorded-by-work-ended, capability-handler-run-by-the-runner); this
holds every host model to a signal, or to why it has none.
"""

from django.apps import apps
from django.test import SimpleTestCase

from hq.platform.core.audit import audited_labels

# Each host model no audit signal watches, and why no change to its rows is a
# person's change of their own to record, so a write to a model nobody
# registered is found here rather than passing as an act that changed nothing.
UNWATCHED = {
    "analytics.AnalyticsCoverage": "a day a credential read, a reading",
    "analytics.AnalyticsSite": "a site as the credential reports it, a reading",
    "analytics.RumDaily": "a day of traffic, a reading",
    "analytics.VitalsDaily": "a day of web vitals, a reading",
    "calendars.Preference": "a person arranging their own month, recorded as the capability's act",
    "control_plane.CertificateMaterial": "beside the sealed secret its upload writes, whose row records it",
    "control_plane.InboundEvent": "what a source said happened, kept for the routine window; its asks are recorded",
    "control_plane.OperationHost": "an index of the machines a run's input names",
    "control_plane.ProviderConnection": "an endpoint as a controller reports it, a reading",
    "control_plane.ProviderInventory": "what a controller read, a reading",
    "control_plane.RefusedReport": "a call the bridge refused, kept until the next is accepted",
    "core.ActionItemRead": "where a person has read to, written by hand with its act",
    "core.AuditLog": "the log itself",
    "core.IdempotencyRecord": "a retry key's first answer",
    "core.Pin": "a person's pins, written by hand with its act",
    "core.Revision": "a table's write count, moved by a trigger",
    "core.ServerError": "a failure HQ logged, kept beside the log line, never a person's act",
    "core.UpstreamReading": "a reading, recorded as observed when it changes",
    "jobs.Job": "a job's one row is its end (audit.work_ended)",
    "search_index.SearchDocument": "an index derived from the records",
}


class WatchedModelTests(SimpleTestCase):
    def test_every_host_model_is_watched_or_says_why_not(self):
        watched = audited_labels()
        unwatched = {
            model._meta.label
            for model in apps.get_models()
            if model.__module__.startswith("hq.") and model not in watched
        }

        self.assertEqual(
            sorted(unwatched - set(UNWATCHED)),
            [],
            "Register each with register_audit(Model, label) in its app's ready(), so a change to its rows is "
            "recorded, or name it in UNWATCHED with why no change to it is a person's own.",
        )
        self.assertEqual(sorted(set(UNWATCHED) - unwatched), [], "Each listed model is a host model no signal watches.")

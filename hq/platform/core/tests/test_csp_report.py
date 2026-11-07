"""The endpoint a browser reports a refused policy to: what it keeps, and how much a stranger can make it write."""

import json

from django.test import Client, TestCase, override_settings

from hq.platform.core.models import AuditLog


def _reports():
    return AuditLog.objects.filter(object_type="ContentSecurityPolicy")


@override_settings(SEVERINO_TRUSTED_NETWORKS=["100.64.0.0/10", "127.0.0.0/8"])
class PolicyReportTests(TestCase):
    """The one boundary HQ cannot check from the inside."""

    def setUp(self):
        self.client = Client(REMOTE_ADDR="100.64.0.1")

    def report(self, payload, content_type="application/csp-report"):
        return self.client.post("/csp-report/", data=payload, content_type=content_type)

    def violations(self, *bodies):
        """One Reporting API post of ``bodies``."""

        sent = [{"type": "csp-violation", "body": body} for body in bodies]
        return self.report(json.dumps(sent), content_type="application/reports+json")

    def test_a_violation_is_recorded_without_a_session(self):
        response = self.report(
            '{"csp-report": {"effective-directive": "script-src",'
            ' "blocked-uri": "https://evil.example/x.js",'
            ' "document-uri": "https://hq.example/"}}'
        )

        self.assertEqual(response.status_code, 204)
        event = _reports().get()
        self.assertEqual(event.metadata["directive"], "script-src")
        self.assertEqual(event.metadata["blocked"], "https://evil.example")

    def test_the_reporting_api_shape_is_read_too(self):
        self.violations({"effectiveDirective": "img-src", "blockedURL": "https://evil.example/x.png"})

        self.assertEqual(_reports().count(), 1)

    def test_one_report_not_in_a_list_is_read_too(self):
        # As WebKit sends it to report-uri: a single Reporting API report,
        # under the legacy content type.
        self.report(
            json.dumps(
                {
                    "type": "csp-violation",
                    "url": "https://hq.example.test/",
                    "body": {
                        "documentURL": "https://hq.example.test/",
                        "disposition": "enforce",
                        "referrer": "",
                        "effectiveDirective": "style-src-elem",
                        "blockedURL": "inline",
                        "statusCode": 200,
                        "sample": "",
                    },
                }
            )
        )

        self.assertEqual(_reports().get().metadata["directive"], "style-src-elem")

    def test_a_directive_the_policy_governs_only_by_fallback_is_recorded(self):
        self.violations({"effectiveDirective": "script-src-elem", "blockedURL": "inline"})

        self.assertEqual(_reports().get().metadata["directive"], "script-src-elem")

    def test_the_legacy_violated_directive_is_read_by_its_name(self):
        self.report('{"csp-report": {"violated-directive": "style-src \'self\'", "blocked-uri": "inline"}}')

        self.assertEqual(_reports().get().metadata["directive"], "style-src")

    def test_a_directive_the_policy_does_not_declare_is_not_recorded(self):
        for directive in ("made-up-src", "report-uri", "a" * 150):
            with self.subTest(directive=directive):
                self.violations({"effectiveDirective": directive, "blockedURL": "inline"})

        self.assertFalse(_reports().exists())

    def test_what_was_blocked_is_kept_as_an_origin_or_a_keyword(self):
        cases = {
            "https://evil.example/a/b.js?q=1#f": "https://evil.example",
            "https://user:pw@evil.example:8443/x": "https://evil.example:8443",
            "http://[2001:db8::1]/x": "http://[2001:db8::1]",
            "inline": "inline",
            "EVAL": "eval",
            "data:image/png;base64,AAAA": "data",
            "blob:https://hq.example/uuid": "blob",
            "trusted-types-sink": "trusted-types-sink",
            "made-up-scheme:x": "",
            "not a url at all": "",
        }
        for sent, kept in cases.items():
            with self.subTest(sent=sent):
                self.violations({"effectiveDirective": "img-src", "blockedURL": sent})
                self.assertEqual(_reports().latest("id").metadata["blocked"], kept)

    def test_distinct_blocked_values_write_no_more_rows_than_the_cap(self):
        def flood(start):
            for batch in range(start, start + 1_000, 10):
                self.violations(
                    *(
                        {"effectiveDirective": "img-src", "blockedURL": f"https://n{n}.example/x.png"}
                        for n in range(batch, batch + 10)
                    )
                )
            return _reports().count()

        first = flood(0)
        second = flood(1_000)

        self.assertGreater(first, 0)
        self.assertEqual(second, first)
        self.assertLessEqual(first, 100)

    def test_the_same_complaint_does_not_write_a_row_per_page_load(self):
        for _ in range(5):
            self.report('{"csp-report": {"effective-directive": "script-src", "blocked-uri": "inline"}}')

        self.assertEqual(_reports().count(), 1)

    def test_nonsense_is_discarded_without_comment(self):
        for payload in ("not json", "[]", "{}", '{"csp-report": {}}', '"a"'):
            with self.subTest(payload=payload):
                response = self.report(payload)
                self.assertEqual(response.status_code, 204)

        self.assertFalse(_reports().exists())

    def test_a_flood_cannot_be_used_to_write_a_large_row(self):
        self.report('{"csp-report": {"effective-directive": "%s"}}' % ("a" * 32_000))

        self.assertFalse(_reports().exists())

    def test_it_is_not_a_readable_surface(self):
        self.assertEqual(self.client.get("/csp-report/").status_code, 405)

    def test_it_is_still_behind_the_network_gate(self):
        response = Client(REMOTE_ADDR="203.0.113.9").post(
            "/csp-report/", data="{}", content_type="application/csp-report"
        )

        self.assertEqual(response.status_code, 403)

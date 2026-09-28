"""Tests for Cloudflare: DNS records and zones, the API client, analytics and the registrar."""

from __future__ import annotations

import json
from unittest import TestCase, mock

from . import providers
from controller_runtime import (
    cloudflare,
    cloudflare_analytics,
    cloudflare_api,
    connection_env,
    provider_http,
)
from control_plane.provider_adapters.contracts import (
    CREDENTIAL_REFUSAL,
    PERMISSION_REFUSAL,
    ProviderError,
)
from control_plane.provider_adapters.parts import part_ledger
from datetime import date
import urllib.request
from .test_support import _Page


CLOUDFLARE_ENV = {
    "CLOUDFLARE_DNS_URL": "https://api.cloudflare.com/client/v4",
    "CLOUDFLARE_DNS_API_TOKEN": "secret-c",
    "CLOUDFLARE_DNS_CONNECTION_REF": "cloudflare-dns-example",
}

ZONE = {
    "id": "zone1",
    "name": "example.com",
    "status": "active",
    "plan": {"name": "Free"},
}


def live(record_id, rtype, name, content, **extra):
    """A record shaped exactly as Cloudflare returns one.

    The field set was read off the real API rather than assumed: `priority` is
    present and null except on MX, CAA carries a formatted `content` *and* a
    `data` object, and a proxied record always reports ttl 1.
    """

    record = {
        "id": record_id,
        "type": rtype,
        "name": name,
        "content": content,
        "proxied": extra.get("proxied", False),
        "ttl": extra.get("ttl", 1),
        "priority": extra.get("priority"),
        "data": extra.get("data"),
    }
    return record


@mock.patch.dict("os.environ", CLOUDFLARE_ENV, clear=True)
class CloudflareAdapterTests(TestCase):
    """The code that actually changes public DNS.

    Every case here is one where getting it wrong is expensive and quiet: a
    record edited into existence twice, a sibling deleted along with its
    neighbour, or a record that reports as drifted against itself and is
    rewritten on every pass forever.
    """

    def setUp(self):
        # Module-level cache of zone name -> id. Harmless in a controller run
        # that lasts a second; between tests it would carry one case's zones
        # into the next.
        cloudflare._ZONE_IDS.clear()

    def _calls(self, request):
        return [call.args[0] for call in request.call_args_list]

    @mock.patch("controller_runtime.cloudflare_api.cloudflare_request")
    def test_a_missing_record_is_created(self, request):
        request.side_effect = [
            [ZONE],
            [],
            live("new1", "A", "app.example.com", "203.0.113.1"),
        ]

        result = cloudflare.reconcile_cloudflare_record(
            {
                "zone": "example.com",
                "name": "app.example.com",
                "record_type": "A",
                "content": "203.0.113.1",
                "proxied": False,
                "ttl": 1,
            }
        )

        self.assertTrue(result.changed)
        created = request.call_args_list[-1]
        self.assertEqual(created.kwargs["method"], "POST")
        self.assertEqual(created.kwargs["payload"]["type"], "A")
        self.assertEqual(created.kwargs["payload"]["content"], "203.0.113.1")
        self.assertEqual(result.status["record_id"], "new1")

    @mock.patch("controller_runtime.cloudflare_api.cloudflare_request")
    def test_a_matching_record_is_left_alone(self, request):
        request.side_effect = [
            [ZONE],
            [live("r1", "A", "app.example.com", "203.0.113.1")],
        ]

        result = cloudflare.reconcile_cloudflare_record(
            {
                "zone": "example.com",
                "name": "app.example.com",
                "record_type": "A",
                "content": "203.0.113.1",
                "proxied": False,
                "ttl": 1,
            }
        )

        self.assertFalse(result.changed)
        # Two reads and no write. A reconciler that rewrites an already-correct
        # record burns an API call per pass and hides real changes in the log.
        self.assertEqual(request.call_count, 2)

    @mock.patch("controller_runtime.cloudflare_api.cloudflare_request")
    def test_a_changed_value_updates_that_record_in_place(self, request):
        request.side_effect = [
            [ZONE],
            [live("r1", "A", "app.example.com", "203.0.113.1")],
            live("r1", "A", "app.example.com", "203.0.113.9"),
        ]

        result = cloudflare.reconcile_cloudflare_record(
            {
                "zone": "example.com",
                "name": "app.example.com",
                "record_type": "A",
                "content": "203.0.113.9",
                "proxied": False,
                "ttl": 1,
            },
            observed={"record_id": "r1"},
        )

        self.assertTrue(result.changed)
        written = request.call_args_list[-1]
        self.assertEqual(written.kwargs["method"], "PUT")
        self.assertIn("/dns_records/r1", written.args[0])

    @mock.patch("controller_runtime.cloudflare_api.cloudflare_request")
    def test_retargeting_a_record_moves_it_rather_than_cloning_it(self, request):
        """The bug class that made renaming create a second record.

        Name and value both change at once, so nothing matches by content. The
        record id HQ was last seen holding is the only thing that still
        identifies it: without that this becomes a create, and the old record
        keeps answering with nothing in HQ pointing at it.
        """

        request.side_effect = [
            [ZONE],
            [live("r1", "A", "old.example.com", "203.0.113.1")],
            live("r1", "A", "new.example.com", "203.0.113.7"),
        ]

        result = cloudflare.reconcile_cloudflare_record(
            {
                "zone": "example.com",
                "name": "new.example.com",
                "record_type": "A",
                "content": "203.0.113.7",
                "proxied": False,
                "ttl": 1,
            },
            observed={"record_id": "r1"},
        )

        self.assertTrue(result.changed)
        written = request.call_args_list[-1]
        self.assertEqual(written.kwargs["method"], "PUT")
        self.assertIn("/dns_records/r1", written.args[0])

    @mock.patch("controller_runtime.cloudflare_api.cloudflare_request")
    def test_one_of_nine_records_on_a_name_is_the_one_edited(self, request):
        """A zone apex holds many records. Matching by name would pick a coin toss."""

        siblings = [
            live(
                "c1",
                "CAA",
                "example.com",
                '0 issue "letsencrypt.org"',
                data={"flags": 0, "tag": "issue", "value": "letsencrypt.org"},
            ),
            live(
                "c2",
                "CAA",
                "example.com",
                '0 issuewild "letsencrypt.org"',
                data={"flags": 0, "tag": "issuewild", "value": "letsencrypt.org"},
            ),
            live("m1", "MX", "example.com", "mx01.example.net", priority=10),
            live("m2", "MX", "example.com", "mx02.example.net", priority=20),
        ]
        request.side_effect = [
            [ZONE],
            siblings,
            live("m2", "MX", "example.com", "mx03.example.net", priority=20),
        ]

        cloudflare.reconcile_cloudflare_record(
            {
                "zone": "example.com",
                "name": "example.com",
                "record_type": "MX",
                "content": "mx03.example.net",
                "priority": 20,
                "proxied": False,
                "ttl": 1,
            },
            observed={"record_id": "m2"},
        )

        self.assertIn("/dns_records/m2", request.call_args_list[-1].args[0])

    @mock.patch("controller_runtime.cloudflare_api.cloudflare_request")
    def test_caa_is_sent_as_three_fields_not_as_a_string(self, request):
        """Cloudflare returns CAA as one string and accepts it only as data."""

        request.side_effect = [
            [ZONE],
            [],
            live("c1", "CAA", "example.com", '0 issue "letsencrypt.org"'),
        ]

        cloudflare.reconcile_cloudflare_record(
            {
                "zone": "example.com",
                "name": "example.com",
                "record_type": "CAA",
                "content": '0 issue "letsencrypt.org"',
                "proxied": False,
                "ttl": 1,
            }
        )

        payload = request.call_args_list[-1].kwargs["payload"]
        self.assertEqual(
            payload["data"], {"flags": 0, "tag": "issue", "value": "letsencrypt.org"}
        )
        self.assertNotIn("content", payload)

    @mock.patch("controller_runtime.cloudflare_api.cloudflare_request")
    def test_an_mx_carries_its_priority_and_an_address_record_does_not(self, request):
        request.side_effect = [
            [ZONE],
            [],
            live("m1", "MX", "example.com", "mx.example.net", priority=10),
        ]
        cloudflare.reconcile_cloudflare_record(
            {
                "zone": "example.com",
                "name": "example.com",
                "record_type": "MX",
                "content": "mx.example.net",
                "priority": 10,
                "proxied": False,
                "ttl": 1,
            }
        )
        self.assertEqual(request.call_args_list[-1].kwargs["payload"]["priority"], 10)

        cloudflare._ZONE_IDS.clear()
        request.reset_mock()
        request.side_effect = [
            [ZONE],
            [],
            live("a1", "A", "app.example.com", "203.0.113.1"),
        ]
        cloudflare.reconcile_cloudflare_record(
            {
                "zone": "example.com",
                "name": "app.example.com",
                "record_type": "A",
                "content": "203.0.113.1",
                "proxied": False,
                "ttl": 1,
            }
        )
        payload = request.call_args_list[-1].kwargs["payload"]
        self.assertNotIn("priority", payload)
        # proxied is only sent for the types that can carry it; Cloudflare
        # rejects the field outright on a TXT or MX record.
        self.assertIn("proxied", payload)

    @mock.patch("controller_runtime.cloudflare_api.cloudflare_request")
    def test_a_txt_value_matches_whether_or_not_it_was_typed_quoted(self, request):
        """Cloudflare stores TXT quoted and returns it quoted, always."""

        request.side_effect = [
            [ZONE],
            [live("t1", "TXT", "example.com", '"v=spf1 -all"')],
        ]

        result = cloudflare.reconcile_cloudflare_record(
            {
                "zone": "example.com",
                "name": "example.com",
                "record_type": "TXT",
                "content": "v=spf1 -all",
                "proxied": False,
                "ttl": 1,
            }
        )

        self.assertFalse(result.changed)

    @mock.patch("controller_runtime.cloudflare_api.cloudflare_request")
    def test_a_name_typed_in_capitals_is_not_permanent_drift(self, request):
        """Cloudflare lowercases names, so sending the typed case never matches."""

        request.side_effect = [
            [ZONE],
            [live("a1", "A", "app.example.com", "203.0.113.1")],
        ]

        result = cloudflare.reconcile_cloudflare_record(
            {
                "zone": "example.com",
                "name": "APP.example.com",
                "record_type": "A",
                "content": "203.0.113.1",
                "proxied": False,
                "ttl": 1,
            }
        )

        self.assertFalse(result.changed)

    @mock.patch("controller_runtime.cloudflare_api.cloudflare_request")
    def test_a_caa_value_with_extra_spaces_is_not_permanent_drift(self, request):
        request.side_effect = [
            [ZONE],
            [
                live(
                    "c1",
                    "CAA",
                    "example.com",
                    '0 issue "letsencrypt.org"',
                    data={"flags": 0, "tag": "issue", "value": "letsencrypt.org"},
                )
            ],
        ]

        result = cloudflare.reconcile_cloudflare_record(
            {
                "zone": "example.com",
                "name": "example.com",
                "record_type": "CAA",
                "content": '0  issue   "letsencrypt.org"',
                "proxied": False,
                "ttl": 1,
            }
        )

        self.assertFalse(result.changed)

    @mock.patch("controller_runtime.cloudflare_api.cloudflare_request")
    def test_delete_removes_only_the_record_it_owns(self, request):
        siblings = [
            live("t1", "TXT", "example.com", '"one"'),
            live("t2", "TXT", "example.com", '"two"'),
            live("t3", "TXT", "example.com", '"three"'),
        ]
        request.side_effect = [[ZONE], siblings, None]

        result = cloudflare.delete_cloudflare_record(
            {
                "zone": "example.com",
                "name": "example.com",
                "record_type": "TXT",
                "content": '"two"',
            },
            observed={"record_id": "t2"},
        )

        self.assertTrue(result.changed)
        deleted = request.call_args_list[-1]
        self.assertEqual(deleted.kwargs["method"], "DELETE")
        self.assertIn("/dns_records/t2", deleted.args[0])

    @mock.patch("controller_runtime.cloudflare_api.cloudflare_request")
    def test_deleting_something_already_gone_is_success(self, request):
        """Deletion has to be idempotent: the queue retries a delete that
        applied and then failed to report, and a second attempt finding nothing
        has achieved exactly what was asked."""

        request.side_effect = [[ZONE], []]

        result = cloudflare.delete_cloudflare_record(
            {
                "zone": "example.com",
                "name": "gone.example.com",
                "record_type": "A",
                "content": "203.0.113.1",
            },
        )

        self.assertFalse(result.changed)
        self.assertEqual(request.call_count, 2)

    @mock.patch("controller_runtime.cloudflare_api.cloudflare_request")
    def test_a_zone_the_credential_cannot_see_is_named(self, request):
        request.side_effect = [[ZONE]]

        with self.assertRaisesRegex(ProviderError, "elsewhere.example"):
            cloudflare.reconcile_cloudflare_record(
                {
                    "zone": "elsewhere.example",
                    "name": "app.elsewhere.example",
                    "record_type": "A",
                    "content": "203.0.113.1",
                    "proxied": False,
                    "ttl": 1,
                }
            )

    @mock.patch("controller_runtime.cloudflare_api.cloudflare_request")
    def test_every_page_of_a_long_zone_is_read(self, request):
        """Cloudflare returns 100 records at most.

        A zone that outgrew one page would have its tail reported as absent, and
        absent is the word this system acts on: the reconciler would set about
        recreating records that were there all along.
        """

        first = [
            live(f"r{i}", "A", f"h{i}.example.com", "203.0.113.1") for i in range(100)
        ]
        second = [live("r100", "A", "h100.example.com", "203.0.113.1")]
        request.side_effect = [[ZONE], first, second]

        records = cloudflare.list_cloudflare_records()

        self.assertEqual(len(records), 101)
        self.assertIn("page=2", self._calls(request)[-1])

    @mock.patch("controller_runtime.cloudflare_api.cloudflare_request")
    def test_the_inventory_reports_what_hq_can_express(self, request):
        request.side_effect = [
            [ZONE],
            [ZONE],
            [live("m1", "MX", "example.com", "mx.example.net", priority=10)],
        ]

        zones = cloudflare.list_cloudflare_zones()
        records = cloudflare.list_cloudflare_records()

        self.assertEqual(zones[0]["zone"], "example.com")
        self.assertEqual(zones[0]["connection_ref"], "cloudflare-dns-example")
        self.assertEqual(records[0]["record_id"], "m1")
        self.assertEqual(records[0]["priority"], 10)
        self.assertEqual(records[0]["zone"], "example.com")


class CloudflareAnalyticsTests(TestCase):
    @mock.patch("controller_runtime.cloudflare_api.cloudflare_api_request")
    def test_account_lists_are_read_to_the_last_page(self, request):
        request.side_effect = [
            {"result": [{"id": f"account-{index}"} for index in range(100)]},
            {"result": [{"id": "account-100"}]},
        ]

        accounts = cloudflare_api.cloudflare_api_list("/accounts", "example-api")

        self.assertEqual(len(accounts), 101)
        self.assertIn("page=1", request.call_args_list[0].args[0])
        self.assertIn("page=2", request.call_args_list[1].args[0])
        self.assertEqual(request.call_args_list[1].args[1], "example-api")

    def test_a_refused_credential_is_not_used_again_in_the_sweep(self):
        import io
        import urllib.error

        refused = urllib.error.HTTPError(
            "u", 403, "Forbidden", {},
            io.BytesIO(b'{"success": false, "errors": [{"code": 9109, '
                       b'"message": "Cannot use the access token from location: 192.0.2.1"}]}'),
        )
        env = {"CF_CONNECTION_REF": "example-api", "CF_PROVIDER": "cloudflare_api",
               "CF_API_TOKEN": "t"}
        with (
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch.object(urllib.request, "urlopen", side_effect=refused) as urlopen,
            provider_http.provider_snapshot(),
        ):
            for path in ("/accounts", "/zones", "/accounts/a/d1/database"):
                with self.assertRaises(ProviderError):
                    cloudflare_api.cloudflare_api_request(path)

        self.assertEqual(urlopen.call_count, 1)

    def test_a_missing_permission_does_not_stop_the_credential(self):
        import io
        import urllib.error

        def refuse(*args, **kwargs):
            return urllib.error.HTTPError(
                "u", 403, "Forbidden", {},
                io.BytesIO(b'{"success": false, "errors": [{"code": 10000, '
                           b'"message": "Authentication error"}]}'),
            )

        env = {"CF_CONNECTION_REF": "example-api", "CF_PROVIDER": "cloudflare_api",
               "CF_API_TOKEN": "t"}
        with (
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch.object(
                urllib.request, "urlopen", side_effect=lambda *a, **k: (_ for _ in ()).throw(refuse())
            ) as urlopen,
            provider_http.provider_snapshot(),
        ):
            for path in ("/accounts", "/zones"):
                with self.assertRaises(ProviderError):
                    cloudflare_api.cloudflare_api_request(path)

        self.assertEqual(urlopen.call_count, 2)

    @staticmethod
    def _per_endpoint(verify_body, status=401):
        """Refuse every call with 10000 except the token verification."""

        import io
        import urllib.error

        def respond(request, timeout=None, context=None):
            if request.full_url.endswith("/user/tokens/verify"):
                return _Page(verify_body, landed=request.full_url)
            raise urllib.error.HTTPError(
                "u", status, "Refused", {},
                io.BytesIO(b'{"success": false, "errors": [{"code": 10000, '
                           b'"message": "Authentication error"}]}'),
            )

        return respond

    def test_an_authentication_error_under_401_is_a_permission_when_the_token_verifies(self):
        verified = (
            b'{"success": true, "result": {"id": "t1", "status": "active", '
            b'"expires_on": "2030-01-01T00:00:00Z"}}'
        )
        env = {"CF_CONNECTION_REF": "example-api", "CF_PROVIDER": "cloudflare_api",
               "CF_API_TOKEN": "t"}
        with (
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch.object(
                urllib.request, "urlopen", side_effect=self._per_endpoint(verified)
            ) as urlopen,
            provider_http.provider_snapshot(),
        ):
            for path in ("/accounts/a/access/apps", "/accounts/a/d1/database"):
                with self.assertRaises(ProviderError) as raised:
                    cloudflare_api.cloudflare_api_request(path, "example-api")
                self.assertEqual(raised.exception.refusal, PERMISSION_REFUSAL)
            refused = dict(cloudflare_api._refused_credentials())

        self.assertEqual(refused, {})
        # Two refused reads and one verification, shared by both.
        self.assertEqual(urlopen.call_count, 3)

    def test_an_authentication_error_under_401_is_the_credential_when_it_does_not_verify(self):
        unverified = b'{"success": false, "errors": [{"code": 1000, "message": "Invalid API Token"}]}'
        env = {"CF_CONNECTION_REF": "example-api", "CF_PROVIDER": "cloudflare_api",
               "CF_API_TOKEN": "t"}
        with (
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch.object(
                urllib.request, "urlopen", side_effect=self._per_endpoint(unverified)
            ),
            provider_http.provider_snapshot(),
        ):
            with self.assertRaises(ProviderError) as raised:
                cloudflare_api.cloudflare_api_request("/accounts/a/access/apps", "example-api")

        self.assertEqual(raised.exception.refusal, CREDENTIAL_REFUSAL)

    def test_the_probe_reports_the_token_expiry(self):
        def respond(request, timeout=None, context=None):
            if request.full_url.endswith("/user/tokens/verify"):
                body = (
                    b'{"success": true, "result": {"status": "active", '
                    b'"expires_on": "2030-01-01T00:00:00Z"}}'
                )
            else:
                body = b'{"success": true, "result": [], "result_info": {"total_pages": 1}}'
            return _Page(body, landed=request.full_url)

        env = {"CLOUDFLARE_DNS_CONNECTION_REF": "example-dns",
               "CLOUDFLARE_DNS_API_TOKEN": "t"}
        with (
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch.object(urllib.request, "urlopen", side_effect=respond),
        ):
            probed = cloudflare._probe_cloudflare_dns("example-dns")

        self.assertEqual(probed["expires_at"], "2030-01-01T00:00:00Z")

    def test_a_connection_reports_where_its_credential_is_kept(self):
        env = {
            "CLOUDFLARE_DNS_CONNECTION_REF": "example-dns",
            "CLOUDFLARE_DNS_API_TOKEN": "secret-value",
            "CLOUDFLARE_DNS_STORE_VAULT": "Example Vault",
            "CLOUDFLARE_DNS_STORE_ITEM": "item-1",
            "CLOUDFLARE_DNS_BOOTSTRAP": "op://Operator/Example bootstrap",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            store = connection_env.connection_store("CLOUDFLARE_DNS")

        self.assertEqual(
            store,
            {"vault": "Example Vault", "item": "item-1",
             "bootstrap": "op://Operator/Example bootstrap"},
        )
        self.assertNotIn("secret-value", json.dumps(store))

    BOTH = {
        "CF_CONNECTION_REF": "example-api",
        "CF_PROVIDER": "cloudflare_api",
        "CF_API_TOKEN": "t",
        "CLOUDFLARE_DNS_CONNECTION_REF": "example-dns",
        "CLOUDFLARE_DNS_API_TOKEN": "t",
    }

    @staticmethod
    def _answer(status, body):
        import io
        import urllib.error

        def respond(request, timeout=None, context=None):
            if status == 200:
                return _Page(body, landed=request.full_url)
            raise urllib.error.HTTPError("u", status, "Refused", {}, io.BytesIO(body))

        return respond

    def test_a_refusal_answered_as_a_200_trips_the_breaker(self):
        body = b'{"success": false, "errors": [{"code": 1000, "message": "Invalid API Token"}]}'
        with (
            mock.patch.dict("os.environ", self.BOTH, clear=True),
            mock.patch.object(
                urllib.request, "urlopen", side_effect=self._answer(200, body)
            ) as urlopen,
            provider_http.provider_snapshot(),
        ):
            for path in ("/accounts", "/zones"):
                with self.assertRaises(ProviderError) as raised:
                    cloudflare_api.cloudflare_api_request(path)
                self.assertEqual(raised.exception.refusal, CREDENTIAL_REFUSAL)

        self.assertEqual(urlopen.call_count, 1)

    def test_the_dns_probe_and_analytics_query_consult_and_record_the_breaker(self):
        body = b'{"success": false, "errors": [{"message": "Invalid API Token"}]}'
        with (
            mock.patch.dict("os.environ", self.BOTH, clear=True),
            mock.patch.object(
                urllib.request, "urlopen", side_effect=self._answer(401, body)
            ) as urlopen,
            provider_http.provider_snapshot(),
        ):
            for call in (
                lambda: cloudflare._probe_cloudflare_dns("example-dns"),
                lambda: cloudflare_analytics._cloudflare_graphql("{ viewer }", {}, "example-api"),
            ):
                for _ in range(2):
                    with self.assertRaises(ProviderError) as raised:
                        call()
                    self.assertEqual(
                        raised.exception.refusal, CREDENTIAL_REFUSAL
                    )
            refused = dict(cloudflare_api._refused_credentials())

        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(set(refused), {"CF", "CLOUDFLARE_DNS"})

    def test_a_revoked_credential_is_refused_once_per_sweep(self):
        """Connections, inventory and analytics share one sweep's breaker."""

        body = b'{"success": false, "errors": [{"message": "Invalid API Token"}]}'
        with (
            mock.patch.dict("os.environ", self.BOTH, clear=True),
            mock.patch.object(
                urllib.request, "urlopen", side_effect=self._answer(401, body)
            ) as urlopen,
            provider_http.provider_snapshot(),
        ):
            found = providers.connections()
            providers.inventory()
            with self.assertRaises(ProviderError):
                cloudflare_analytics._cloudflare_graphql("{ viewer }", {}, "example-api")

        self.assertEqual(urlopen.call_count, 2)
        self.assertFalse(any(connection["ok"] for connection in found))

    def test_the_api_url_defaults_to_cloudflare_and_can_be_overridden(self):
        with mock.patch.dict(
            "os.environ",
            {"CF_CONNECTION_REF": "example-api", "CF_PROVIDER": "cloudflare_api"},
            clear=True,
        ):
            self.assertEqual(
                cloudflare_api.cloudflare_url(provider="cloudflare_api"),
                cloudflare_api.CLOUDFLARE_API_URL,
            )
        with mock.patch.dict(
            "os.environ",
            {"CF_CONNECTION_REF": "example-api", "CF_PROVIDER": "cloudflare_api",
             "CF_URL": "https://cloudflare.example.test/client/v4/"},
            clear=True,
        ):
            self.assertEqual(
                cloudflare_api.cloudflare_url(provider="cloudflare_api"),
                "https://cloudflare.example.test/client/v4",
            )

    @mock.patch("controller_runtime.cloudflare_analytics.analytics_account")
    @mock.patch("controller_runtime.cloudflare_api.cloudflare_api_request")
    def test_registrations_are_read_by_cursor(self, request, account):
        account.return_value = "account-id"
        request.side_effect = [
            {
                "result": [{"domain_name": "Example.com.", "expires_at": "2027-01-02T00:00:00Z",
                            "auto_renew": True, "locked": True, "status": "active"}],
                "result_info": {"cursor": "next/page=="},
            },
            {
                "result": [{"domain_name": "example.net", "expires_at": "2026-12-01T00:00:00Z",
                            "auto_renew": False, "status": "active"}],
                "result_info": {"cursor": ""},
            },
        ]

        found = cloudflare._registrar_domains()

        self.assertIn("/accounts/account-id/registrar/registrations?", request.call_args_list[0].args[0])
        self.assertIn("cursor=next%2Fpage%3D%3D", request.call_args_list[1].args[0])
        self.assertEqual(found["example.com"]["expires_at"], "2027-01-02")
        self.assertTrue(found["example.com"]["auto_renew"])
        self.assertFalse(found["example.net"]["auto_renew"])

    @mock.patch("controller_runtime.cloudflare_analytics.analytics_account")
    @mock.patch("controller_runtime.cloudflare_api.cloudflare_api_request")
    def test_a_refused_registrar_read_carries_its_reason(self, request, account):
        account.return_value = "account-id"
        request.side_effect = ProviderError("Cloudflare refused: 403")

        with part_ledger() as refused:
            found = cloudflare._registrar_domains()

        self.assertEqual(found, {})
        self.assertEqual([entry["reason"] for entry in refused], ["Cloudflare refused: 403"])

    @mock.patch("controller_runtime.cloudflare_analytics.account_sites")
    @mock.patch("controller_runtime.cloudflare_analytics.analytics_account")
    @mock.patch("controller_runtime.cloudflare_api.cloudflare_api_request")
    def test_probe_and_reader_share_account_discovery(self, request, account, sites):
        request.return_value = {"success": True}
        account.return_value = "account-id"
        sites.return_value = [{"site_tag": "0" * 32, "host": "example.test"}]

        result = cloudflare._probe_cloudflare_api("example-api")

        account.assert_called_once_with("example-api")
        sites.assert_called_once_with("account-id", "example-api")
        self.assertEqual(result["reaches"], ["example.test"])

    @mock.patch("controller_runtime.cloudflare_analytics._cloudflare_graphql")
    def test_site_reading_uses_its_connection(self, graphql):
        graphql.return_value = {"viewer": {"accounts": [{}]}}

        result = cloudflare_analytics._analytics_site_reading(
            "account-id",
            {"site_tag": "0" * 32, "host": "example.test"},
            "account-two",
            start=date(2026, 8, 26),
            end=date(2026, 8, 27),
            query="query Analytics {}",
        )

        self.assertEqual(result["connection_ref"], "account-two")
        self.assertEqual(graphql.call_args.args[2], "account-two")

    @mock.patch("controller_runtime.cloudflare_analytics._analytics_site_reading")
    @mock.patch("controller_runtime.cloudflare_analytics.analytics_sites")
    def test_every_configured_connection_is_read(self, sites, reading):
        sites.return_value = [
            {
                "account": "account-id-one",
                "connection_ref": "account-one",
                "site_tag": "1" * 32,
                "host": "one.example",
            },
            {
                "account": "account-id-two",
                "connection_ref": "account-two",
                "site_tag": "2" * 32,
                "host": "two.example",
            },
        ]
        reading.side_effect = [
            {"connection_ref": "account-one"},
            {"connection_ref": "account-two"},
        ]

        result = cloudflare_analytics.analytics()

        self.assertEqual(
            [site["connection_ref"] for site in result["sites"]],
            ["account-one", "account-two"],
        )
        self.assertEqual(
            [call.args[2] for call in reading.call_args_list],
            ["account-one", "account-two"],
        )

    @mock.patch("controller_runtime.cloudflare_analytics._analytics_site_reading")
    @mock.patch(
        "controller_runtime.cloudflare_analytics.completed_window",
        return_value=(date(2026, 8, 26), date(2026, 8, 28)),
    )
    def test_hq_can_plan_an_exact_window_for_each_site(self, _window, reading):
        site = {
            "account": "account-id",
            "connection_ref": "account-one",
            "site_tag": "1" * 32,
            "host": "one.example",
        }
        reading.return_value = {"connection_ref": "account-one"}

        cloudflare_analytics.analytics(
            sites=[site],
            windows=[
                {
                    "connection_ref": "account-one",
                    "site_tag": "1" * 32,
                    "start": "2026-06-01",
                    "end": "2026-08-28",
                }
            ],
        )

        self.assertEqual(reading.call_args.kwargs["start"], date(2026, 6, 1))
        self.assertEqual(reading.call_args.kwargs["end"], date(2026, 8, 28))

    @mock.patch("controller_runtime.cloudflare_analytics._analytics_site_reading")
    @mock.patch(
        "controller_runtime.cloudflare_analytics.completed_window",
        return_value=(date(2026, 8, 26), date(2026, 8, 28)),
    )
    def test_an_invalid_plan_falls_back_to_the_shared_safe_window(
        self, _window, reading
    ):
        site = {
            "account": "account-id",
            "connection_ref": "account-one",
            "site_tag": "1" * 32,
            "host": "one.example",
        }
        reading.return_value = {}

        cloudflare_analytics.analytics(
            sites=[site],
            windows=[
                {
                    "connection_ref": "account-one",
                    "site_tag": "1" * 32,
                    "start": "not-a-date",
                    "end": None,
                }
            ],
        )

        self.assertEqual(reading.call_args.kwargs["start"], date(2026, 8, 26))
        self.assertEqual(reading.call_args.kwargs["end"], date(2026, 8, 28))

    @mock.patch("controller_runtime.cloudflare_analytics._cloudflare_graphql")
    def test_missing_graphql_account_fails_closed(self, graphql):
        graphql.return_value = {"viewer": {"accounts": []}}

        with self.assertRaisesRegex(ProviderError, "matching account"):
            cloudflare_analytics._analytics_site_reading(
                "account-id",
                {"site_tag": "0" * 32, "host": "example.test"},
                "example-api",
                start=date(2026, 8, 26),
                end=date(2026, 8, 27),
                query="query Analytics {}",
            )


class RegistrarRefusalTests(TestCase):
    def test_a_refused_registrar_read_says_which_refusal(self):
        refused = ProviderError(
            "Cloudflare refused the request: Authentication error", refusal="permission"
        )
        with mock.patch.object(cloudflare_analytics, "analytics_account", return_value="acct"), \
                mock.patch.object(cloudflare_api, "cloudflare_api_cursor_list", side_effect=refused):
            with part_ledger() as refused_parts:
                found = cloudflare._registrar_domains()

        self.assertEqual(found, {})
        self.assertEqual(
            refused_parts,
            [{"part": "registration", "refusal": "permission",
              "reason": "Cloudflare refused the request: Authentication error",
              "scope": "", "connection_ref": ""}],
        )

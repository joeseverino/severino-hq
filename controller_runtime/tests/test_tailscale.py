"""Tests for the tailnet: devices, routes, the policy and what the tailnet reports."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

from .. import providers
from controller_runtime import handlers, tailnet_api, tailnet_policy, tailscale
from control_plane.provider_adapters.contracts import (
    CREDENTIAL_REFUSAL,
    PERMISSION_REFUSAL,
    ProviderError,
)
from control_plane.provider_adapters.parts import part_ledger
import urllib.request
from .test_support import _Answer, _controller_source


class AppConnectorTests(TestCase):
    """An app connector is a node routing traffic for named domains on the
    tailnet's behalf: a way something is reached that is neither a device nor
    a DNS record, declared inside the policy rather than anywhere HQ looked."""

    def connectors(self, policy):
        return tailnet_policy._app_connectors(policy)

    def test_a_declared_connector_is_read_out_of_the_policy(self):
        found = self.connectors(
            {
                "nodeAttrs": [
                    {
                        "target": ["tag:server"],
                        "app": {
                            "tailscale.com/app-connectors": [
                                {
                                    "name": "example-connector",
                                    "connectors": ["tag:server"],
                                    "domains": ["example.test"],
                                }
                            ]
                        },
                    }
                ]
            }
        )

        self.assertEqual(
            found,
            [
                {
                    "name": "example-connector",
                    "connectors": ["tag:server"],
                    "domains": ["example.test"],
                }
            ],
        )

    def test_other_node_attributes_are_not_mistaken_for_connectors(self):
        """`nodeAttrs` carries every per-node attribute the policy sets."""

        found = self.connectors({"nodeAttrs": [{"target": ["*"], "attr": ["funnel"]}]})

        self.assertEqual(found, [])

    def test_a_policy_with_no_attributes_at_all_reads_as_none(self):
        self.assertEqual(self.connectors({}), [])


class TailnetSweepTests(TestCase):
    """Reading the tailnet through the daemon this node is already a peer of.

    Every field is optional. Tailscale omits rather than nulls (a device with
    expiry disabled carries no ``KeyExpiry`` and one never seen carries no
    ``LastSeen``) so a reader that requires any of them rejects exactly the
    devices it exists to describe.
    """

    STATUS = {
        "Self": {"HostName": "this-node", "Online": True, "OS": "linux"},
        "Peer": {
            "k1": {
                "HostName": "an-edge",
                "DNSName": "an-edge.example.ts.net.",
                "Online": True,
                "KeyExpiry": "2026-11-04T00:00:00Z",
                "TailscaleIPs": ["100.64.0.2"],
                "OS": "linux",
                "ExitNode": True,
                "ExitNodeOption": True,
            },
            "k2": {
                "HostName": "a-tv",
                "Online": False,
                "LastSeen": "2026-07-01T00:00:00Z",
            },
        },
    }

    def sweep(self, status=None, *, path=None):
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as directory:
            reading = Path(directory) / "tailnet.json"
            reading.write_text(
                json.dumps(self.STATUS if status is None else status), encoding="utf-8"
            )
            given = str(reading) if path is None else path
            with mock.patch.object(tailscale, "TAILNET_STATUS", given):
                return tailscale.list_tailnet_devices()

    def by_name(self):
        return {record["name"]: record for record in self.sweep()}

    def test_the_node_itself_is_one_of_the_machines(self):
        self.assertIn("this-node", self.by_name())

    def test_presence_comes_across(self):
        found = self.by_name()
        self.assertTrue(found["an-edge"]["online"])
        self.assertFalse(found["a-tv"]["online"])

    def test_offering_to_be_an_exit_node_is_not_being_the_one_in_use(self):
        """Two different questions. `ExitNode` is whether this peer is the exit
        node the reading machine currently routes through: a fact about our
        own preference. `ExitNodeOption` is whether the peer offers to be one.
        A machine page saying "exit node" means the second."""

        found = self.by_name()

        self.assertTrue(found["an-edge"]["offers_exit_node"])
        self.assertTrue(found["an-edge"]["exit_node_in_use"])
        # A device that offers nothing says so on both counts.
        self.assertFalse(found["a-tv"]["offers_exit_node"])
        self.assertFalse(found["a-tv"]["exit_node_in_use"])

    def test_a_key_expiry_is_carried_and_its_absence_is_not_invented(self):
        found = self.by_name()
        self.assertEqual(found["an-edge"]["key_expires"], "2026-11-04T00:00:00Z")
        self.assertEqual(found["a-tv"]["key_expires"], "")

    def test_a_device_missing_every_optional_field_is_still_read(self):
        records = self.sweep({"Self": {"HostName": "bare"}, "Peer": {}})

        self.assertEqual(records[0]["name"], "bare")

    def test_a_nameless_device_is_dropped_rather_than_named_blank(self):
        records = self.sweep({"Self": {}, "Peer": {"k": {"HostName": ""}}})

        self.assertEqual(records, [])

    def test_a_controller_given_no_reading_says_so(self):
        """A machine not on a tailnet is a supported way to be."""

        from unittest import mock

        with mock.patch.object(tailscale, "TAILNET_STATUS", ""):
            with self.assertRaises(ProviderError) as raised:
                tailscale.list_tailnet_devices()

        self.assertIn("not given a tailnet reading", str(raised.exception))

    def test_with_only_a_credential_devices_come_from_the_api(self):
        from unittest import mock

        listed = [
            {
                "hostname": "example-host",
                "name": "example-host.example.ts.net.",
                "addresses": ["100.64.0.5"],
                "os": "linux",
                "connectedToControl": True,
                "lastSeen": "2026-09-25T00:00:00Z",
                "keyExpiryDisabled": True,
                "tags": ["tag:server"],
                "advertisedRoutes": ["0.0.0.0/0", "::/0"],
                "enabledRoutes": [],
                "clientConnectivity": {"endpoints": ["192.0.2.10:41641"]},
            }
        ]
        with (
            mock.patch.object(tailscale, "TAILNET_STATUS", ""),
            mock.patch.object(tailnet_api, "tailnet_token", return_value="t"),
            mock.patch.object(tailnet_api, "tailnet_api_devices", return_value=listed),
            mock.patch.object(tailnet_policy, "reach_by_device", return_value={}),
        ):
            (device,) = tailscale.list_tailnet_devices()

        self.assertEqual(device["name"], "example-host")
        self.assertEqual(device["dns_name"], "example-host.example.ts.net")
        self.assertEqual(device["addresses"], ["100.64.0.5"])
        self.assertTrue(device["online"])
        self.assertEqual(device["key_expires"], "")
        self.assertEqual(device["tags"], ["tag:server"])
        self.assertTrue(device["offers_exit_node"])
        self.assertFalse(device["exit_node_approved"])
        self.assertEqual(device["endpoints"], ["192.0.2.10:41641"])

    def test_a_refused_device_list_raises_with_its_scope(self):
        import io
        import urllib.error
        from unittest import mock

        refused = urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO(b""))
        with mock.patch.object(urllib.request, "urlopen", side_effect=refused):
            with self.assertRaises(ProviderError) as raised:
                tailnet_api.tailnet_api_devices("t")

        self.assertIn("devices:core:read", str(raised.exception))

    def test_a_missing_reading_is_reported_rather_than_crashing(self):
        with self.assertRaises(ProviderError) as raised:
            self.sweep(path="/nonexistent/tailnet.json")

        self.assertIn("missing", str(raised.exception))

    def test_the_controller_never_holds_the_daemon_socket(self):
        """The local API is read and write, and this only ever needed reading.

        Asserted on the module rather than trusted: the socket was mounted into
        this container once, and the process that reads it holds every provider
        credential HQ has.
        """

        source = _controller_source()

        self.assertNotIn("tailscaled.sock", source)

    def test_an_unreachable_daemon_does_not_take_the_sweep_down(self):
        """One provider that cannot be read must not lose the other five."""

        from unittest import mock

        with (
            mock.patch.dict("os.environ", {"TAILSCALE_CONNECTION_REF": "example-tailnet"}),
            mock.patch.object(
                tailscale, "list_tailnet_devices", side_effect=ProviderError("no")
            ),
        ):
            found = providers.inventory()

        self.assertFalse(found["tailscale.device"]["ok"])
        self.assertEqual(found["tailscale.device"]["records"], [])


class TailnetDeviceTests(TestCase):
    """Asserting HQ's one decision about a device, and nothing else about it."""

    STATUS = {
        "Self": {"HostName": "this-node", "ID": "nSELF", "Online": True},
        "Peer": {
            "k1": {
                "HostName": "an-edge",
                "ID": "nEDGE",
                "Online": True,
                "KeyExpiry": "2026-11-04T00:00:00Z",
            },
            "k2": {"HostName": "a-server", "ID": "nSERV", "Online": True},
        },
    }

    def setUp(self):
        import tempfile

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        reading = Path(self.directory.name) / "tailnet.json"
        reading.write_text(json.dumps(self.STATUS), encoding="utf-8")
        patch = mock.patch.object(tailscale, "TAILNET_STATUS", str(reading))
        patch.start()
        self.addCleanup(patch.stop)

    def spec(self, name="an-edge", disabled=True):
        return {
            "connection_ref": "a-tailnet",
            "name": name,
            "key_expiry_disabled": disabled,
        }

    def test_a_device_already_as_declared_is_left_alone(self):
        """a-server has no expiry, so there is nothing to assert."""

        result = tailscale.reconcile_tailnet_device(self.spec("a-server"))

        self.assertFalse(result.changed)

    def test_a_dry_run_says_what_it_would_do_and_does_not_do_it(self):
        with mock.patch.object(tailnet_api, "tailnet_token") as token:
            result = tailscale.reconcile_tailnet_device(self.spec(), apply=False)

        self.assertTrue(result.changed)
        token.assert_not_called()

    def test_the_device_id_comes_from_the_local_reading_not_the_api(self):
        """The token is spent on the change and on nothing else."""

        self.assertEqual(tailscale._tailnet_device_id("an-edge"), "nEDGE")

    def test_a_device_the_tailnet_does_not_show_is_refused_by_name(self):
        with self.assertRaises(ProviderError) as raised:
            tailscale.reconcile_tailnet_device(self.spec("a-ghost"))

        self.assertIn("a-ghost", str(raised.exception))

    def test_a_credential_without_the_scope_says_which_scope(self):
        import urllib.error

        refused = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
        with (
            mock.patch.object(tailnet_api, "tailnet_token", return_value="t"),
            mock.patch.object(urllib.request, "urlopen", side_effect=refused),
            self.assertRaises(ProviderError) as raised,
        ):
            tailscale.reconcile_tailnet_device(self.spec())

        self.assertIn("devices:core", str(raised.exception))

    def test_an_api_key_used_as_an_oauth_client_says_so(self):
        import urllib.error

        refused = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
        with (
            mock.patch.dict(
                os.environ,
                {
                    "TAILNET_CONNECTION_REF": "a-tailnet",
                    "TAILNET_PROVIDER": "tailscale",
                    "TAILNET_CLIENT_ID": "id",
                    "TAILNET_CLIENT_SECRET": "secret",
                },
            ),
            mock.patch.object(urllib.request, "urlopen", side_effect=refused),
            self.assertRaises(ProviderError) as raised,
        ):
            tailnet_api.tailnet_token("a-tailnet")

        self.assertIn("OAuth client", str(raised.exception))

    def test_a_malformed_oauth_response_fails_as_a_safe_provider_error(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"[]"
        with (
            mock.patch.dict(
                os.environ,
                {
                    "TAILNET_CONNECTION_REF": "a-tailnet",
                    "TAILNET_PROVIDER": "tailscale",
                    "TAILNET_CLIENT_ID": "id",
                    "TAILNET_CLIENT_SECRET": "secret",
                },
            ),
            mock.patch.object(
                urllib.request, "urlopen", return_value=response
            ),
            self.assertRaises(ProviderError) as raised,
        ):
            tailnet_api.tailnet_token("a-tailnet")

        self.assertEqual(
            str(raised.exception), "Tailscale did not answer the token request."
        )
        self.assertNotIn("secret", str(raised.exception))

    def test_the_token_is_never_kept(self):
        """It lasts an hour and a sweep is minutes apart, so caching it would
        only add an expiry to get wrong."""

        source = _controller_source()

        self.assertNotIn("_TOKEN_CACHE", source)
        self.assertEqual(source.count("def tailnet_token"), 1)


class TailnetPolicyGateTests(TestCase):
    """Validation runs only the tests a document carries, so it is not a gate alone."""

    TEST = {"src": "a-laptop", "accept": ["a-server:443"]}

    def document(self, *tests):
        return {"grants": [{"src": ["*"], "dst": ["*"], "ip": ["*"]}], "tests": list(tests)}

    def reconcile(self, live, declared):
        spec = {"connection_ref": "a-tailnet", "document": json.dumps(declared)}
        with (
            mock.patch.object(tailnet_api, "tailnet_token", return_value="t"),
            mock.patch.object(tailnet_policy, "_tailnet_policy", return_value=live),
            mock.patch.object(urllib.request, "urlopen") as urlopen,
        ):
            urlopen.return_value.__enter__.return_value.read.return_value = b"{}"
            result = tailnet_policy.reconcile_tailnet_policy(spec, apply=False)
        return result, urlopen

    def test_a_policy_with_no_tests_is_refused_before_tailscale_is_asked(self):
        with self.assertRaises(ProviderError) as raised:
            self.reconcile(self.document(self.TEST), self.document())

        self.assertIn("no tests", str(raised.exception))

    def test_a_testless_live_policy_does_not_license_another(self):
        """The ratchet has to start somewhere: zero is never a baseline."""

        with self.assertRaises(ProviderError):
            self.reconcile(self.document(), {"grants": [], "tests": []})

    def test_a_testless_policy_already_in_place_is_not_ready(self):
        result, _ = self.reconcile(self.document(), self.document())

        self.assertFalse(result.changed)
        self.assertFalse(result.conditions[0]["status"])
        self.assertEqual(result.conditions[0]["reason"], "Untested")

    def test_dropping_a_deny_is_refused(self):
        other = {"src": "a-laptop", "deny": ["a-server:22"]}
        with self.assertRaises(ProviderError) as raised:
            self.reconcile(self.document(self.TEST, other), self.document(self.TEST))

        self.assertIn("'a-server:22'", str(raised.exception))

    def test_a_deny_moved_to_accept_is_refused(self):
        live = {"src": "a-laptop", "deny": ["a-server:22"]}
        opened = {"src": "a-laptop", "accept": ["a-server:22"]}
        with self.assertRaises(ProviderError):
            self.reconcile(
                self.document(self.TEST, live), self.document(self.TEST, opened)
            )

    def test_replacing_a_src_with_another_of_the_same_count_is_refused(self):
        """Same number of tests, but one (src, proto) no longer tested."""

        live = {"src": "a-phone", "proto": "tcp", "accept": ["a-server:443"]}
        swapped = {"src": "a-stranger", "proto": "tcp", "accept": ["a-server:443"]}
        with self.assertRaises(ProviderError) as raised:
            self.reconcile(
                self.document(self.TEST, live), self.document(self.TEST, swapped)
            )

        self.assertIn("'a-phone' over tcp", str(raised.exception))

    def test_a_proto_is_part_of_what_a_test_covers(self):
        live = {"src": "a-laptop", "proto": "udp", "deny": ["a-server:53"]}
        moved = {"src": "a-laptop", "deny": ["a-server:53"]}
        with self.assertRaises(ProviderError):
            self.reconcile(
                self.document(self.TEST, live), self.document(self.TEST, moved)
            )

    def test_tests_may_be_merged_when_every_pair_and_deny_survives(self):
        live = {"src": "a-laptop", "deny": ["a-server:22"]}
        merged = {
            "src": "a-laptop",
            "accept": ["a-server:443"],
            "deny": ["a-server:22", "a-server:23"],
        }
        result, _ = self.reconcile(
            self.document(self.TEST, live), self.document(merged)
        )

        self.assertTrue(result.changed)

    def test_a_rewritten_test_passes_to_validation(self):
        moved = {"src": "a-laptop", "accept": ["a-server:443", "a-server:22"]}
        result, urlopen = self.reconcile(
            self.document(self.TEST), self.document(moved)
        )

        self.assertTrue(result.changed)
        self.assertIn("/acl/validate", urlopen.call_args.args[0].full_url)


class RouteApprovalTests(TestCase):
    """Approving routes is the one tailnet call that writes.

    Tailscale takes the whole enabled set on every write, so what this sends
    decides which routes keep working. A list assembled from anywhere but the
    device's own advertisement would withdraw a route the call was never about
    silently, and on a subnet router that is somebody's network going away.
    """

    def approve(self, reads, *, apply=True):
        """Run the handler against a scripted sequence of API answers."""

        sent = []

        def urlopen(request, timeout=None, context=None):
            del timeout
            sent.append(request)
            return _Answer(reads[len(sent) - 1])

        with (
            mock.patch.object(tailscale, "_tailnet_device_id", return_value="node-1"),
            mock.patch.object(tailnet_api, "tailnet_token", return_value="token"),
            mock.patch.object(urllib.request, "urlopen", urlopen),
        ):
            result = tailscale.approve_tailnet_routes(
                {"name": "a-router", "connection_ref": "a-tailnet"}, apply=apply
            )
        return result, sent

    def test_what_is_approved_is_what_the_device_advertises(self):
        result, sent = self.approve(
            [
                {"advertisedRoutes": ["10.0.0.0/24", "0.0.0.0/0"], "enabledRoutes": []},
                {"enabledRoutes": ["0.0.0.0/0", "10.0.0.0/24"]},
            ]
        )

        self.assertTrue(result.changed)
        self.assertEqual(sent[1].method, "POST")
        self.assertEqual(
            json.loads(sent[1].data)["routes"], ["0.0.0.0/0", "10.0.0.0/24"]
        )
        self.assertEqual(result.status["enabled_routes"], ["0.0.0.0/0", "10.0.0.0/24"])

    def test_an_already_enabled_route_is_kept_rather_than_withdrawn(self):
        """The write is the whole set, so approving the second route has to
        send the first one back with it."""

        _, sent = self.approve(
            [
                {
                    "advertisedRoutes": ["10.0.0.0/24", "10.0.1.0/24"],
                    "enabledRoutes": ["10.0.0.0/24"],
                },
                {"enabledRoutes": ["10.0.0.0/24", "10.0.1.0/24"]},
            ]
        )

        self.assertIn("10.0.0.0/24", json.loads(sent[1].data)["routes"])

    def test_nothing_pending_writes_nothing(self):
        result, sent = self.approve(
            [
                {"advertisedRoutes": ["10.0.0.0/24"], "enabledRoutes": ["10.0.0.0/24"]},
            ]
        )

        self.assertFalse(result.changed)
        self.assertEqual(len(sent), 1)
        self.assertEqual(result.message, "Nothing to approve.")

    def test_a_device_offering_nothing_is_a_no_op_not_a_clearance(self):
        """An empty advertisement must not become an empty write: that is how
        a handler meant to approve routes ends up removing them."""

        result, sent = self.approve([{"advertisedRoutes": [], "enabledRoutes": []}])

        self.assertFalse(result.changed)
        self.assertEqual(len(sent), 1)
        self.assertIn("advertises no routes", result.message)

    def test_a_plan_says_what_it_would_do_and_touches_nothing(self):
        result, sent = self.approve(
            [{"advertisedRoutes": ["10.0.0.0/24"], "enabledRoutes": []}], apply=False
        )

        self.assertTrue(result.changed)
        self.assertEqual(len(sent), 1)
        self.assertIn("Would approve 10.0.0.0/24", result.message)

    def refuse_at(self, call, code):
        """Answer normally until `call`, then refuse with `code`."""

        reads = [{"advertisedRoutes": ["10.0.0.0/24"], "enabledRoutes": []}]
        calls = []

        def urlopen(request, timeout=None, context=None):
            del timeout
            calls.append(request)
            if len(calls) == call:
                raise urllib.error.HTTPError(
                    "https://example.invalid", code, "Forbidden", {}, None
                )
            return _Answer(reads[len(calls) - 1])

        with (
            mock.patch.object(tailscale, "_tailnet_device_id", return_value="node-1"),
            mock.patch.object(tailnet_api, "tailnet_token", return_value="token"),
            mock.patch.object(urllib.request, "urlopen", urlopen),
            self.assertRaises(ProviderError) as raised,
        ):
            tailscale.approve_tailnet_routes({"name": "a-router"})
        return str(raised.exception)

    def test_a_credential_without_the_scope_says_which_scope(self):
        """Each call names the scope it needs: the read devices:routes:read,
        the approval devices:routes. Neither is devices:core."""

        for code in (401, 403):
            with self.subTest(code=code):
                read = self.refuse_at(1, code)
                write = self.refuse_at(2, code)
                self.assertIn("devices:routes:read", read)
                self.assertIn("devices:routes scope", write)
                self.assertNotIn("devices:core", read + write)

    def test_a_refusal_that_is_not_about_scope_is_not_reported_as_one(self):
        self.assertNotIn("devices:routes", self.refuse_at(1, 500))
        self.assertNotIn("devices:routes", self.refuse_at(2, 500))

    def test_an_unreadable_device_is_not_approved_blind(self):
        def urlopen(request, timeout=None, context=None):
            del request, timeout
            raise OSError("no route to host")

        with (
            mock.patch.object(tailscale, "_tailnet_device_id", return_value="node-1"),
            mock.patch.object(tailnet_api, "tailnet_token", return_value="token"),
            mock.patch.object(urllib.request, "urlopen", urlopen),
            self.assertRaisesRegex(
                ProviderError, "did not report the routes"
            ),
        ):
            tailscale.approve_tailnet_routes({"name": "a-router"})


class _Scripted:
    """urlopen answering each call in turn; an exception in the script is raised."""

    def __init__(self, script):
        self.script = list(script)
        self.sent = []

    def __call__(self, request, timeout=None, context=None):
        del timeout, context
        self.sent.append(request)
        answer = self.script[len(self.sent) - 1]
        if isinstance(answer, BaseException):
            raise answer
        body, etag = answer if isinstance(answer, tuple) else (answer, "")
        response = _Answer(body)
        response.headers = {"etag": etag}
        return response


def _refused(code):
    return urllib.error.HTTPError(
        "https://example.invalid", code, "Refused", {}, None
    )


class TailnetPolicyWriteTests(TestCase):
    """Every outcome of a policy reconcile, pinned to what it says and sends."""

    TESTED = {"grants": [], "tests": [{"src": "a-laptop", "accept": ["a-server:443"]}]}
    WANTED = {
        "grants": [{"src": ["*"], "dst": ["*"], "ip": ["*"]}],
        "tests": [{"src": "a-laptop", "accept": ["a-server:443"]}],
    }

    def reconcile(self, document, script=(), *, live=None, apply=True):
        urlopen = _Scripted(script)
        with (
            mock.patch.object(tailnet_api, "tailnet_token", return_value="t"),
            mock.patch.object(tailnet_policy, "_tailnet_policy", return_value=live),
            mock.patch.object(urllib.request, "urlopen", urlopen),
        ):
            result = tailnet_policy.reconcile_tailnet_policy(
                {"connection_ref": "a-tailnet", "document": document}, apply=apply
            )
        return result, urlopen.sent

    def test_no_declared_policy_reads_nothing(self):
        result, sent = self.reconcile("  ")

        self.assertFalse(result.changed)
        self.assertEqual(result.conditions, [])
        self.assertIn("No policy is declared", result.message)
        self.assertEqual(sent, [])

    def test_an_unreadable_declaration_is_refused(self):
        with self.assertRaisesRegex(ProviderError, "not readable JSON"):
            self.reconcile("{not json")

    def test_a_tested_policy_already_in_place_is_ready(self):
        result, sent = self.reconcile(json.dumps(self.TESTED), live=self.TESTED)

        self.assertFalse(result.changed)
        self.assertEqual(result.status, {"applied": True})
        self.assertEqual(
            [(c["type"], c["status"], c["reason"]) for c in result.conditions],
            [("Ready", True, "Reconciled")],
        )
        self.assertEqual(result.message, "Tailnet policy is current.")
        self.assertEqual(sent, [])

    def test_a_policy_failing_its_own_tests_is_not_written(self):
        with self.assertRaisesRegex(ProviderError, "does not pass its own"):
            self.reconcile(
                json.dumps(self.WANTED), [{"errors": ["x"]}], live=self.TESTED
            )

    def test_a_validation_that_cannot_run_is_not_a_pass(self):
        with self.assertRaisesRegex(ProviderError, "could not check"):
            self.reconcile(json.dumps(self.WANTED), [_refused(500)], live=self.TESTED)

    def test_the_write_is_conditional_on_the_version_read(self):
        result, sent = self.reconcile(
            json.dumps(self.WANTED), [{}, ({}, '"v1"'), {}], live=self.TESTED
        )

        self.assertTrue(result.changed)
        self.assertEqual(result.status, {"applied": True})
        self.assertEqual(result.message, "Tailnet policy applied after its own tests passed.")
        self.assertEqual(
            [request.full_url.rsplit("/", 1)[-1] for request in sent],
            ["validate", "acl", "acl"],
        )
        self.assertEqual(sent[2].get_method(), "POST")
        self.assertEqual(sent[2].get_header("If-match"), '"v1"')
        self.assertEqual(json.loads(sent[2].data), self.WANTED)

    def test_no_version_read_writes_without_a_condition(self):
        _, sent = self.reconcile(
            json.dumps(self.WANTED), [{}, _refused(500), {}], live=self.TESTED
        )

        self.assertIsNone(sent[2].get_header("If-match"))

    def test_a_policy_changed_elsewhere_is_not_overwritten(self):
        with self.assertRaisesRegex(ProviderError, "changed somewhere else"):
            self.reconcile(
                json.dumps(self.WANTED), [{}, ({}, "v"), _refused(412)], live=self.TESTED
            )

    def test_other_write_failures_say_what_happened(self):
        with self.assertRaisesRegex(ProviderError, r"refused the policy \(403\)"):
            self.reconcile(
                json.dumps(self.WANTED), [{}, ({}, "v"), _refused(403)], live=self.TESTED
            )
        with self.assertRaisesRegex(ProviderError, "did not answer the policy"):
            self.reconcile(
                json.dumps(self.WANTED), [{}, ({}, "v"), OSError()], live=self.TESTED
            )


class RouteApprovalFailureTests(TestCase):
    def approve(self, script):
        with (
            mock.patch.object(tailscale, "_tailnet_device_id", return_value="node-1"),
            mock.patch.object(tailnet_api, "tailnet_token", return_value="token"),
            mock.patch.object(urllib.request, "urlopen", _Scripted(script)),
        ):
            return tailscale.approve_tailnet_routes({"name": "a-router"})

    def test_a_write_that_gets_no_answer_says_so(self):
        pending = {"advertisedRoutes": ["10.0.0.0/24"], "enabledRoutes": []}
        with self.assertRaisesRegex(ProviderError, "did not answer for a-router"):
            self.approve([pending, OSError()])

    def test_a_refused_write_that_is_not_about_scope_names_the_device(self):
        pending = {"advertisedRoutes": ["10.0.0.0/24"], "enabledRoutes": []}
        with self.assertRaisesRegex(
            ProviderError, "refused the route approval for a-router"
        ):
            self.approve([pending, _refused(500)])

    def test_an_approval_reports_the_set_tailscale_answered_with(self):
        result = self.approve(
            [
                {"advertisedRoutes": ["10.0.1.0/24", "10.0.0.0/24"], "enabledRoutes": None},
                {"enabledRoutes": ["10.0.1.0/24"]},
            ]
        )

        self.assertEqual(
            result.status,
            {
                "name": "a-router",
                "advertised_routes": ["10.0.0.0/24", "10.0.1.0/24"],
                "enabled_routes": ["10.0.1.0/24"],
            },
        )
        self.assertEqual(result.message, "Approved 10.0.0.0/24, 10.0.1.0/24 for a-router.")
        self.assertEqual(
            [(c["type"], c["reason"]) for c in result.conditions], [("Ready", "Reconciled")]
        )


class TailnetReadingTests(TestCase):
    """Tailnet-level readings: kept fields only, and a refusal names its scope."""

    def read(self, kind, payload):
        from control_plane.observations import OBSERVATIONS

        calls = []

        def urlopen(request, timeout=None, context=None):
            del timeout
            calls.append(request.full_url)
            return _Answer(payload)

        with (
            mock.patch.object(tailnet_api, "tailnet_token", return_value="token"),
            mock.patch.object(urllib.request, "urlopen", urlopen),
        ):
            records = handlers.OBSERVATION_READERS[kind]()
        kept, refused = OBSERVATIONS[kind].clean(records)
        self.assertEqual(refused, 0)
        return kept, calls

    def refuse(self, kind, code):
        def urlopen(request, timeout=None, context=None):
            del request, timeout
            raise urllib.error.HTTPError(
                "https://example.invalid", code, "Forbidden", {}, None
            )

        with (
            mock.patch.object(tailnet_api, "tailnet_token", return_value="token"),
            mock.patch.object(urllib.request, "urlopen", urlopen),
            self.assertRaises(ProviderError) as raised,
        ):
            handlers.OBSERVATION_READERS[kind]()
        self.refusal = raised.exception.refusal
        return str(raised.exception)

    DNS = {
        "nameservers": [
            {"address": "100.64.0.53", "useWithExitNode": True},
            {"address": "192.0.2.53"},
        ],
        "splitDNS": {
            "corp.example.com": [{"address": "192.0.2.54"}],
            "empty.example.com": None,
        },
        "searchPaths": ["example.com"],
        "preferences": {"magicDNS": True, "overrideLocalDNS": True},
        "extraField": "never stored",
    }

    def test_dns_is_read_from_the_configuration_endpoint(self):
        (record,), calls = self.read("tailscale.dns", self.DNS)

        self.assertEqual(calls, [f"{tailnet_api.TAILNET_API}/tailnet/-/dns/configuration"])
        self.assertEqual(record["nameservers"], ["100.64.0.53", "192.0.2.53"])
        self.assertTrue(record["magic_dns"])
        self.assertTrue(record["override_local_dns"])
        self.assertEqual(record["search_paths"], ["example.com"])
        self.assertEqual(
            record["split_dns"],
            {"corp.example.com": ["192.0.2.54"], "empty.example.com": []},
        )
        self.assertNotIn("extraField", record)

    def test_dns_joins_on_every_resolver_address(self):
        from control_plane.observations import OBSERVATIONS

        (record,), _ = self.read("tailscale.dns", self.DNS)

        self.assertEqual(
            tuple(OBSERVATIONS["tailscale.dns"].addresses(record)),
            ("100.64.0.53", "192.0.2.53", "192.0.2.54"),
        )

    SETTINGS = {
        "devicesApprovalOn": True,
        "devicesKeyDurationDays": 90,
        "devicesAutoUpdatesOn": False,
        "usersApprovalOn": True,
        "networkFlowLoggingOn": False,
        "regionalRoutingOn": False,
        "postureIdentityCollectionOn": False,
        "httpsEnabled": True,
        "aclsExternallyManagedOn": True,
        "aclsExternalLink": "https://example.com/policy",
        "usersRoleAllowedToJoinExternalTailnets": "none",
    }

    def test_settings_keep_the_named_settings_only(self):
        (record,), calls = self.read("tailscale.settings", self.SETTINGS)

        self.assertEqual(calls, [f"{tailnet_api.TAILNET_API}/tailnet/-/settings"])
        self.assertEqual(record["devices_key_duration_days"], 90)
        self.assertTrue(record["devices_approval_on"])
        self.assertTrue(record["acls_externally_managed_on"])
        self.assertNotIn("unread", record)
        self.assertNotIn("aclsExternalLink", record)
        self.assertNotIn("acls_external_link", record)
        self.assertNotIn("network_flow_logging_on", record)

    def test_a_setting_another_scope_governs_is_its_part_refused(self):
        from control_plane.reading_parts import clean_refused_parts, refused_parts

        partial = {**self.SETTINGS, "aclsExternallyManagedOn": None}
        del partial["httpsEnabled"]

        with part_ledger() as ledger:
            (record,), _ = self.read("tailscale.settings", partial)
        snapshot = SimpleNamespace(
            kind="tailscale.settings", reachable=True,
            refused_parts=clean_refused_parts("tailscale.settings", ledger),
        )
        missing = {refused.part.name: refused.missing for refused in refused_parts(snapshot)}

        self.assertIsNone(record["https_enabled"])
        self.assertNotIn("unread", record)
        self.assertEqual(missing, {"https": ("networking_settings:read",),
                                   "acl_management": ("policy_file:read",)})

    def test_every_setting_seen_refuses_no_part(self):
        with part_ledger() as ledger:
            self.read("tailscale.settings", self.SETTINGS)

        self.assertEqual(ledger, [])

    USERS = {
        "users": [
            {
                "id": "u1",
                "displayName": "Example Person",
                "loginName": "person@example.com",
                "profilePicUrl": "https://example.com/p.png",
                "tailnetId": "t1",
                "created": "2026-01-01T00:00:00Z",
                "type": "member",
                "role": "owner",
                "status": "active",
                "deviceCount": 3,
                "lastSeen": "2026-09-01T00:00:00Z",
                "currentlyConnected": True,
            },
            {"displayName": "no id"},
        ]
    }

    def test_users_keep_identity_role_and_presence_only(self):
        (record,), calls = self.read("tailscale.user", self.USERS)

        self.assertEqual(calls, [f"{tailnet_api.TAILNET_API}/tailnet/-/users"])
        self.assertEqual(
            record,
            {
                "id": "u1",
                "display_name": "Example Person",
                "login_name": "person@example.com",
                "role": "owner",
                "status": "active",
                "created": "2026-01-01T00:00:00Z",
                "last_seen": "2026-09-01T00:00:00Z",
            },
        )

    def test_clean_drops_fields_the_schema_does_not_name(self):
        from control_plane.observations import OBSERVATIONS

        kept, _ = OBSERVATIONS["tailscale.user"].clean(
            [{"id": "u1", "profile_pic_url": "https://example.com/p.png"}]
        )

        self.assertEqual(kept, [{"id": "u1"}])

    def test_a_refused_read_raises_and_names_its_scope(self):
        for kind, scope in (
            ("tailscale.dns", "dns:read"),
            ("tailscale.settings", "feature_settings:read"),
            ("tailscale.user", "users:read"),
        ):
            for code in (403, 404):
                with self.subTest(kind=kind, code=code):
                    self.assertIn(scope, self.refuse(kind, code))
                    self.assertEqual(self.refusal, PERMISSION_REFUSAL)

    def test_a_refused_token_is_the_credential_not_a_scope(self):
        message = self.refuse("tailscale.user", 401)

        self.assertNotIn("users:read", message)
        self.assertEqual(self.refusal, CREDENTIAL_REFUSAL)

    def test_a_failure_that_is_not_about_scope_raises_without_one(self):
        message = self.refuse("tailscale.dns", 500)

        self.assertIn("500", message)
        self.assertNotIn("dns:read", message)

    def test_an_unreadable_answer_raises_rather_than_reading_as_empty(self):
        with (
            mock.patch.object(tailnet_api, "tailnet_token", return_value="token"),
            mock.patch.object(
                urllib.request, "urlopen", return_value=_Answer([])
            ),
            self.assertRaises(ProviderError),
        ):
            tailscale.list_tailnet_users()

    def test_services_are_read_from_the_services_endpoint(self):
        calls = []

        def urlopen(request, timeout=None, context=None):
            del timeout
            calls.append(request.full_url)
            if request.full_url.endswith("/services"):
                return _Answer(
                    {
                        "vipServices": [
                            {
                                "name": "svc:example",
                                "addrs": ["100.64.0.9"],
                                "ports": ["tcp:443"],
                            }
                        ]
                    }
                )
            return _Answer({})

        with (
            mock.patch.object(tailnet_api, "tailnet_token", return_value="token"),
            mock.patch.object(urllib.request, "urlopen", urlopen),
        ):
            (record,) = tailnet_policy.list_tailnet_policy()

        self.assertEqual(record["services"][0]["name"], "svc:example")
        self.assertFalse(any("vip-services" in url for url in calls))

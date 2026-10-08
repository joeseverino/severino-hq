"""The request path: HQ's own path joined to what one request shows.

Most of these take a fact away from the request, or put a wrong one in, and
assert the hop says so: a check that can only come back proven is decoration.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, ProviderInventory

from ..path_model import Hop
from ..paths import hq_path
from ..projection import projection_scope
from ..request_path import (
    CONTRADICTED,
    LAYER_STEPS,
    PROVEN,
    UNPROVEN,
    _Context,
    _edge,
    request_path,
    serialize_request_path,
)

HQ = "hq.example.test"
LAPTOP = "100.64.0.20"
HQ_TAILNET = "100.64.0.10"
HQ_LAN = "192.0.2.10"
PROXY = "172.18.0.2"


def an_estate():
    """HQ behind a proxy host on its machine, named by an internal rewrite."""

    ManagedResource.objects.create(
        key="hq-host",
        kind="machine",
        spec={"name": "hq-host", "addresses": [HQ_LAN, HQ_TAILNET]},
    )
    ManagedResource.objects.create(
        key="hq-proxy",
        kind="npm.proxy_host",
        spec={
            "domain_names": [HQ],
            "forward_scheme": "http",
            "forward_host": HQ_LAN,
            "forward_port": 8000,
        },
    )
    now = timezone.now()
    ProviderInventory.objects.create(
        kind="adguard.rewrite",
        observed_at=now,
        records=[{"domain": HQ, "answer": HQ_TAILNET}],
    )
    ProviderInventory.objects.create(
        kind="tailscale.device",
        observed_at=now,
        records=[
            {
                "name": "hq-host",
                "addresses": [HQ_TAILNET],
                "self": True,
                "public_key": "test-key-hq",
            },
            {
                "name": "a-laptop",
                "addresses": [LAPTOP],
                "os": "macOS",
                "user": "someone@example.test",
                "public_key": "test-key-laptop",
                "last_handshake": now.isoformat(),
                "direct_endpoint": "198.51.100.7:41641",
            },
        ],
    )


def a_request(*, peer=PROXY, forwarded=LAPTOP, **headers):
    extra = {"HTTP_X_FORWARDED_FOR": forwarded} if forwarded else {}
    request = RequestFactory().get(
        "/connection/", secure=True, HTTP_HOST=HQ, REMOTE_ADDR=peer, **extra, **headers
    )
    request.user = get_user_model()(username="someone", email="someone@example.test")
    return request


NPM_HEADERS = {"HTTP_X_REAL_IP": LAPTOP, "HTTP_X_FORWARDED_SCHEME": "https"}


@override_settings(
    ALLOWED_HOSTS=[HQ, "testserver"],
    SEVERINO_SITE_HOST=HQ,
    SEVERINO_TRUSTED_PROXIES=["172.18.0.0/16"],
)
class RequestPathTests(TestCase):
    def setUp(self):
        an_estate()
        own = mock.patch(
            "hq.platform.application.hq_self.host_addresses", return_value=frozenset({HQ_LAN})
        )
        own.start()
        self.addCleanup(own.stop)

    def walk(self, request):
        with projection_scope():
            return request_path(request)

    def checks(self, request):
        return {hop.step: hop.check.state for hop in self.walk(request).hops if hop.check}

    def test_a_request_through_the_proxy_proves_the_path(self):
        found = self.walk(a_request(**NPM_HEADERS))

        self.assertEqual(
            [hop.step for hop in found.hops],
            ["device", "dns", "network", "machine", "ingress", "machine", "hq"],
        )
        states = {hop.step: hop.check.state for hop in found.hops if hop.check}
        for step in ("device", "dns", "network", "ingress", "hq"):
            self.assertEqual(states[step], PROVEN, step)
        self.assertEqual(found.findings, ())

    def test_the_device_hop_names_the_device_and_whose_it_is(self):
        device = self.walk(a_request(**NPM_HEADERS)).hops[0]

        self.assertEqual(device.name, "a-laptop")
        self.assertIn("macOS", device.detail)
        self.assertEqual(device.source.kind, "tailscale.device")

    def test_the_tailnet_leg_is_read_from_the_callers_presence(self):
        network = next(hop for hop in self.walk(a_request(**NPM_HEADERS)).hops if hop.step == "network")

        self.assertEqual(network.name, "Direct")
        self.assertIn("handshake", network.detail)

    def test_an_idle_link_is_not_called_no_path_on_the_request_that_used_it(self):
        devices = ProviderInventory.objects.get(kind="tailscale.device")
        for record in devices.records:
            record.pop("direct_endpoint", None)
        devices.save()

        network = next(hop for hop in self.walk(a_request(**NPM_HEADERS)).hops if hop.step == "network")

        self.assertEqual(network.check.state, PROVEN)
        self.assertEqual(network.name, "Connected")

    def test_a_request_that_skipped_the_proxy_is_a_finding(self):
        found = self.walk(a_request(peer=LAPTOP, forwarded=""))

        ingress = next(hop for hop in found.hops if hop.step == "ingress")
        self.assertEqual(ingress.check.state, CONTRADICTED)
        self.assertIn("reached HQ straight from", ingress.check.detail)
        self.assertTrue(ingress.check.step)
        self.assertIn(ingress, found.findings)

    def test_a_tailnet_address_no_device_holds_is_a_finding(self):
        states = self.checks(a_request(forwarded="100.64.0.99"))

        self.assertEqual(states["device"], CONTRADICTED)

    def test_a_request_from_the_lan_for_a_tailnet_name_is_a_finding(self):
        states = self.checks(a_request(forwarded="10.0.0.50"))

        self.assertEqual(states["network"], CONTRADICTED)

    def test_an_untrusted_forwarder_contradicts_the_device_and_the_proxy(self):
        states = self.checks(a_request(peer="10.9.9.9"))

        self.assertEqual(states["device"], CONTRADICTED)
        self.assertEqual(states["ingress"], CONTRADICTED)

    def test_disagreeing_proxy_headers_contradict_the_proxy(self):
        states = self.checks(
            a_request(HTTP_X_REAL_IP="100.64.0.77", HTTP_X_FORWARDED_SCHEME="https")
        )

        self.assertEqual(states["ingress"], CONTRADICTED)

    def test_a_trusted_proxy_without_its_own_headers_is_not_proof_of_which_proxy(self):
        self.assertEqual(self.checks(a_request())["ingress"], UNPROVEN)

    def test_the_machine_is_proven_only_by_where_hq_answered(self):
        found = self.walk(a_request(**NPM_HEADERS))
        machines = [hop.check.state for hop in found.hops if hop.step == "machine"]
        self.assertIn(PROVEN, machines)

        with mock.patch("hq.platform.application.hq_self.host_addresses", return_value=frozenset()):
            found = self.walk(a_request(**NPM_HEADERS))
        self.assertNotIn(PROVEN, [hop.check.state for hop in found.hops if hop.step == "machine"])

    def test_every_layer_is_on_exactly_one_hop_and_the_hop_of_its_step(self):
        found = self.walk(a_request(**NPM_HEADERS))
        placed = [layer.id for hop in found.route.hops for layer in hop.layers]

        self.assertEqual(sorted(placed), sorted(layer.id for layer in found.connection.layers))
        steps = {hop.step for hop in found.route.hops}
        for hop in found.route.hops:
            for layer in hop.layers:
                wanted = LAYER_STEPS[layer.id]
                self.assertEqual(hop.step, wanted if wanted in steps else "hq", layer.id)

    def test_every_layer_the_request_can_produce_has_a_step(self):
        from ..connection_security import SecurityControl

        control = SecurityControl("edge", "Ingress policy", "good", "", "")
        firewall = SecurityControl("host-firewall", "Arrival", "good", "", "")
        from ..connection import connection

        found = connection(a_request(**NPM_HEADERS), edge=control, firewall=firewall)

        self.assertLessEqual({layer.id for layer in found.layers}, set(LAYER_STEPS))

    def test_the_forwarded_chain_is_attached_where_each_address_belongs(self):
        found = self.walk(a_request(**NPM_HEADERS))
        roles = {hop.step: [item.role for item in hop.evidence if item.role] for hop in found.hops}

        self.assertEqual(roles["device"], ["judged"])
        self.assertEqual(roles["ingress"], ["proxy"])
        self.assertEqual([item.value for item in found.chain], [LAPTOP, PROXY])

    def test_the_page_path_is_hq_path(self):
        request = a_request(**NPM_HEADERS)
        with projection_scope():
            page = request_path(request).path
            walked = hq_path(request)

        self.assertEqual(
            [(hop.step, hop.name) for hop in page.primary.hops],
            [(hop.step, hop.name) for hop in walked.primary.hops],
        )

    def test_a_request_to_a_deployment_with_no_name_still_has_a_path(self):
        with override_settings(SEVERINO_SITE_HOST="", ALLOWED_HOSTS=["*"]):
            found = self.walk(a_request(**NPM_HEADERS))

        self.assertEqual([hop.step for hop in found.hops], ["device", "hq"])

    def test_the_projection_names_no_secret(self):
        request = a_request(
            HTTP_COOKIE="sessionid=planted-cookie-value",
            HTTP_AUTHORIZATION="Bearer planted-token-value",
            **NPM_HEADERS,
        )
        with projection_scope():
            text = str(serialize_request_path(request_path(request)))

        self.assertNotIn("planted-cookie-value", text)
        self.assertNotIn("planted-token-value", text)


@override_settings(ALLOWED_HOSTS=[HQ, "testserver"])
class AccessTests(TestCase):
    """Access in front of a name: the assertion header, never its value."""

    def setUp(self):
        ProviderInventory.objects.create(
            kind="cloudflare.access_app",
            observed_at=timezone.now(),
            records=[{"id": "a1", "name": "Example admin", "domain": HQ}],
        )

    def judged(self, **headers):
        request = a_request(**headers)
        from ..connection import connection

        with projection_scope():
            return _edge(Hop("edge", "Edge"), _Context(request, connection(request), HQ, ()))

    def test_a_request_without_the_assertion_did_not_pass_access(self):
        _evidence, check = self.judged()

        self.assertEqual(check.state, CONTRADICTED)
        self.assertTrue(check.step)

    def test_an_assertion_is_shown_as_present_and_never_as_its_value(self):
        evidence, check = self.judged(HTTP_CF_ACCESS_JWT_ASSERTION="planted.jwt.value")

        self.assertEqual(check.state, UNPROVEN)
        self.assertNotIn("planted", str(evidence))
        self.assertIn("present", evidence[0].value)


@override_settings(
    ALLOWED_HOSTS=[HQ, "testserver"],
    SEVERINO_SITE_HOST=HQ,
    SEVERINO_TRUSTED_PROXIES=["172.18.0.0/16"],
)
class ReadParityTests(TestCase):
    """The page, the API and MCP answer from one projection."""

    def setUp(self):
        an_estate()

    def test_the_resource_is_the_page_projection_for_the_same_request(self):
        from .. import request_context
        from ..resources import list_resource
        from ..security import Capability, Principal

        reader = Principal("reader", "test", frozenset({Capability.READ}))
        request = a_request(**NPM_HEADERS)
        bound = request_context.bind(request)
        try:
            served = list_resource("request.path", {}, principal=reader)
        finally:
            request_context.unbind(bound)
        from ..hq_self import serving

        with projection_scope(seed=serving(request)):
            page = serialize_request_path(request_path(request))

        self.assertEqual(served, page)

    def test_mcp_serves_the_same_read(self):
        from hq.platform.mcp import services as mcp_services
        from hq.platform.mcp.identity import reset_principal, set_principal

        from .. import request_context
        from ..security import mcp_principal

        request = a_request(**NPM_HEADERS)
        principal = set_principal(mcp_principal())
        bound = request_context.bind(request)
        try:
            served = mcp_services.list_resource("request.path", {})
        finally:
            request_context.unbind(bound)
            reset_principal(principal)

        self.assertEqual(served["items"][0]["step"], "device")
        self.assertEqual(served["items"][-1]["step"], "hq")

    def test_without_a_request_the_read_says_why_it_is_empty(self):
        from ..resources import list_resource
        from ..security import Capability, Principal

        reader = Principal("reader", "test", frozenset({Capability.READ}))
        served = list_resource("request.path", {}, principal=reader)

        self.assertEqual(served["count"], 0)
        self.assertTrue(served["unread"][0].startswith("not read: request, because"))

    def test_the_page_renders_every_hop_and_every_layer(self):
        user = get_user_model().objects.create_user("someone", password="not-used-here")
        self.client.force_login(user)

        response = self.client.get(
            reverse("connection"),
            secure=True,
            HTTP_HOST=HQ,
            REMOTE_ADDR=PROXY,
            HTTP_X_FORWARDED_FOR=LAPTOP,
        )

        found = response.context["request_path"]
        self.assertContains(response, "data-connection-hop=", count=len(found.hops))
        self.assertContains(
            response, "data-connection-layer=", count=len(found.connection.layers)
        )
        self.assertContains(response, "Path to HQ")
        self.assertContains(response, found.proof)
        # The link's facts come from the caller's presence, and still show.
        self.assertContains(response, "198.51.100.7:41641")
        self.assertContains(response, "Last handshake")


class ContainerHopTests(TestCase):
    """HQ can say which container answered: Docker sets a container's
    hostname to its short ID, and the sweep reads that ID."""

    def check(self, *, here, listed):
        from types import SimpleNamespace

        from .. import request_path as module

        machine = SimpleNamespace(
            containers=[SimpleNamespace(name="example-hq", id=listed)] if listed is not None else []
        )
        with mock.patch("hq.platform.application.connections.machines_once", return_value=(machine,)), \
                mock.patch("hq.platform.application.request_path.own_container_id", return_value=here):
            # Built as paths.py builds it: step, kind label, then the name.
            return module._container(Hop("container", "Container", "example-hq"), None)

    def test_its_own_container_is_proven(self):
        evidence, check = self.check(here="0123456789ab", listed="0123456789ab")

        self.assertIn("inside example-hq", check.detail)

        self.assertEqual(check.state, PROVEN)
        self.assertEqual(evidence[0].value, "0123456789ab")

    def test_a_container_the_sweep_has_not_seen_yet_is_unproven_not_wrong(self):
        _, check = self.check(here="ffffffffffff", listed="0123456789ab")

        self.assertEqual(check.state, UNPROVEN)
        self.assertIn("until the next read", check.detail)

    def test_its_own_container_is_read_from_its_mounts_on_any_network(self):
        from .. import request_path as module

        container = "0123456789ab" + "c" * 52
        mountinfo = f"612 598 0:52 /var/lib/docker/containers/{container}/hostname /etc/hostname rw\n"
        with mock.patch("pathlib.Path.open", mock.mock_open(read_data=mountinfo)):
            self.assertEqual(module.own_container_id(), "0123456789ab")
        with mock.patch("pathlib.Path.open", mock.mock_open(read_data="22 1 8:1 / / rw\n")):
            self.assertEqual(module.own_container_id(), "")

    def test_a_sweep_that_read_no_id_says_it_cannot_show(self):
        _, check = self.check(here="0123456789ab", listed="")

        self.assertIn("no container ID", check.detail)

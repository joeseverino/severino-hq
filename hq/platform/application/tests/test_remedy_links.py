"""Every remedy link HQ emits opens its form ready: the target chosen, and only
the notices that apply to what the form does.

A remedy that opens a command with nothing selected looks like it worked and
does nothing; one that opens the "replaces the whole record" trap turns a fix
into a way to blank a record. Both happened. These tests make either
impossible as a class: every command that takes a target preselects any target
it acts on, however far down the catalogue, and every link the findings, the
action queue and the service pages emit is followed and checked.
"""

import re
from html import unescape
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import Resolver404, resolve, reverse

from hq.domains.control_plane.models import ManagedResource
from hq.domains.control_plane.providers import PROVIDERS

from ..action_links import command_url
from ..capabilities import capability_registry
from ..dashboard import work_queue
from ..findings import derive_findings
from ..paths import routed_names
from ..projection import MAX_PAGE_SIZE, projection_scope
from ..security import web_principal
from ..topology import derive_topology
from .test_approvals import POLICY_KEY, declare_policy
from .test_finding_fixes import document
from .test_paths import PUBLIC_RANGE, estate, record, store
from .test_tailnet_posture import policy as tailnet_policy, tailnet_connection

TRAP = "This replaces the whole record"
UNOFFERED = "cannot be used here"


def an_operator():
    return get_user_model().objects.create_user("example-operator", password="x" * 20, is_staff=True, is_superuser=True)


def _kind_for(spec) -> str:
    """A resource kind the command acts on: its own, or any declared kind."""

    wanted = dict(spec.target_query).get("kind")
    return str(wanted) if wanted else "adguard.rewrite"


class EveryTargetedCommandPreselectsTests(TestCase):
    """The capability side of the contract, for every command, not a sample."""

    def setUp(self):
        self.client.force_login(an_operator())

    def test_a_target_past_the_first_page_is_chosen_for_every_resource_command(self):
        # More than the largest page of declarations, all sorting before the
        # target, so only a form that fetches the linked one can offer it.
        ManagedResource.objects.bulk_create(
            ManagedResource(
                key=f"a-{index:03}",
                kind="adguard.rewrite",
                spec={"domain": f"h{index}.example.com", "answer": "192.0.2.1"},
            )
            for index in range(MAX_PAGE_SIZE + 1)
        )
        commands = [
            spec
            for spec in capability_registry().values()
            if spec.subject_resource == "infrastructure.resources" and spec.target_kind
        ]
        self.assertTrue(commands)
        for spec in commands:
            kind = _kind_for(spec)
            if kind not in PROVIDERS:
                continue
            key = f"z-{kind.replace('.', '-')}"
            ManagedResource.objects.get_or_create(key=key, kind=kind, defaults={"spec": {}})
            with self.subTest(command=spec.name):
                response = self.client.get(command_url(spec.name, key))

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, f'<option value="{key}" selected>')
                self.assertNotContains(response, UNOFFERED)

    def test_a_target_the_command_does_not_act_on_is_said_not_silently_dropped(self):
        response = self.client.get(command_url("tailnet.policy.remove_empty_groups", "no-such-thing"))

        self.assertContains(response, f"no-such-thing {UNOFFERED}")


def _command_links(links) -> list[tuple[str, str]]:
    """``(label, url)`` for each link that opens a command form."""

    found = []
    for label, url, method in links:
        if not url or method.upper() != "GET":
            continue
        try:
            match = resolve(urlsplit(url).path)
        except Resolver404:
            found.append((label, url))  # kept, so the follow fails loudly
            continue
        if match.url_name == "command":
            found.append((label, url))
    return found


@PUBLIC_RANGE
class EveryEmittedRemedyOpensReadyTests(TestCase):
    """The emitting side: follow what findings, the queue and the service pages offer."""

    def setUp(self):
        estate()
        # Contradictions with a declaration to repoint.
        store(
            "cloudflare.dns_record",
            record("shop.example.com", "A", "198.51.100.20"),
            record("db.example.com", "A", "198.51.100.20", proxied=False),
        )
        store(
            "adguard.rewrite",
            {"domain": "app.example.com", "answer": "100.64.0.10", "connection_ref": "example-adguard"},
            {"domain": "db.example.com", "answer": "100.64.0.10", "connection_ref": "example-adguard"},
        )
        ManagedResource.objects.create(
            key="example-db-rewrite",
            kind="adguard.rewrite",
            spec={"domain": "db.example.com", "answer": "100.64.0.10", "connection_ref": "example-adguard"},
        )
        ManagedResource.objects.create(
            key="example-shop-proxy",
            kind="npm.proxy_host",
            spec={
                "domain_names": ["shop.example.com"],
                "forward_scheme": "http",
                "forward_host": "198.51.100.20",
                "forward_port": 8080,
                "connection_ref": "example-npm",
            },
        )
        # A drifted tailnet policy with empty groups: keep-live and amend remedies.
        declare_policy(document())
        policy = ManagedResource.objects.get(key=POLICY_KEY)
        policy.conditions = [{"type": "Drifted", "status": True, "reason": "Drifted", "message": "differs"}]
        policy.generation, policy.observed_generation = 2, 1
        policy.save(update_fields=["conditions", "generation", "observed_generation"])
        # The tailnet as read: an empty group and a tag no device wears, granted.
        tailnet_connection()
        tailnet_policy(
            groups=[{"name": "group:empty", "members": []}],
            grants=[{"src": ["group:empty"], "dst": ["tag:gone:443"]}],
        )
        store(
            "tailscale.device",
            {
                "name": "example-device",
                "tags": ["tag:server"],
                "addresses": ["100.64.0.20"],
                "connection_ref": "example-tailnet",
            },
        )
        self.user = an_operator()
        self.client.force_login(self.user)

    def emitted(self) -> list[tuple[str, str, str]]:
        principal = web_principal(self.user)
        links: list[tuple[str, str, str]] = []
        with projection_scope():
            for finding in derive_findings(derive_topology(principal=principal), principal=principal):
                links += [(remedy.label, remedy.url, remedy.method) for remedy in finding.remedies]
                for step in finding.workflow.steps if finding.workflow else ():
                    links += [(action.label, action.url, action.method) for action in step.actions]
            for item in work_queue():
                links += [(action["label"], action["url"], action["method"]) for action in item["actions"]]
                for step in (item["workflow"] or {}).get("steps", ()):
                    links += [(action["label"], action["url"], action["method"]) for action in step.get("actions", ())]
        return links + self.rendered()

    def rendered(self) -> list[tuple[str, str, str]]:
        """Every command link the pages themselves render, as a person meets them."""

        pages = [
            reverse("control_plane:findings"),
            reverse("action_items"),
            *(reverse("control_plane:service", args=[name]) for name in routed_names()),
            *(resource.get_absolute_url() for resource in ManagedResource.objects.all()),
        ]
        found = []
        for page in pages:
            body = self.client.get(page).content.decode()
            for label_url in re.finditer(r'<a [^>]*href="(/commands/[^"]+)"[^>]*>(.*?)</a>', body, re.DOTALL):
                url, label = unescape(label_url.group(1)), re.sub(r"<[^>]+>|\s+", " ", label_url.group(2)).strip()
                found.append((label, url, "GET"))
        return found

    def test_the_estate_emits_command_remedies_to_follow(self):
        labels = {label for label, _url in _command_links(self.emitted())}

        # The fixture is only worth something while it raises these.
        self.assertIn("Change the internal record", labels)
        self.assertIn("Put an access list in front of example-shop-proxy", labels)
        self.assertIn("Keep the live version", labels)
        self.assertIn("Remove empty groups", labels)
        self.assertIn("Change what HQ expects", labels)

    def test_every_command_remedy_opens_with_its_target_chosen_and_no_trap(self):
        followed = _command_links(self.emitted())
        self.assertTrue(followed)
        for label, url in dict.fromkeys(followed):
            with self.subTest(remedy=label, url=url):
                response = self.client.get(url)

                self.assertEqual(response.status_code, 200)
                target = parse_qs(urlsplit(url).query).get("target", [""])[0]
                if target:
                    self.assertContains(response, f'<option value="{target}" selected>')
                self.assertNotContains(response, UNOFFERED)
                self.assertNotContains(response, TRAP)
                self.assertNotContains(response, "<strong>Destructive</strong>")

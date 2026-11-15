"""An image's findings on a machine are one each, however many of its
containers run it, and each finding HQ answers has an id of its own."""

from collections import Counter

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from ..finding_model import Finding
from ..findings import finding_facts, findings
from ..inventory_testing import store
from ..security import Capability, Principal
from .test_container_standard import KEPT, runtime
from .test_containers import running

READER = Principal("reader", "test", frozenset({Capability.READ}))
ADVISORY, UPDATE, UNPINNED = "container-image-advisory", "container-image-update", "container-image-unpinned"
HARDENING = "container-hardening"

PROXY = "quay.io/example/oauth2-proxy"
PROXY_ID = "sha256:" + "8" * 64
WEBKIT, WEBKIT_ID = "example/hermes-webkit", "sha256:" + "3" * 64
INSPECT, INSPECT_ID = "example-inspect", "sha256:" + "7" * 64
KUMA, KUMA_ID = "example/kuma", "sha256:" + "e" * 64
ADVISORIES = [
    {
        "id": f"GHSA-example-{n}",
        "severity": "high",
        "summary": "s",
        "url": "u",
        "vulnerabilities": [["< 7.15.6", "7.15.6"]],
    }
    for n in (1, 2)
]


def image(identity, name, tag, *users):
    """An image as Docker's containerd store lists it, which gives every image
    a repository digest, one built on the machine included."""

    return {
        "connection_ref": "example-portainer",
        "host": "example-box",
        "id": identity,
        "tags": [f"{name}:{tag}"],
        "digests": [f"{name}@{identity}"],
        "containers": [
            {"container": user, "reference": reference, "service": service} for user, reference, service in users
        ],
    }


def project(name, *files):
    return {"connection_ref": "example-portainer", "host": "example-box", "name": name, "config_files": list(files)}


def homelab(**proxy_runtime):
    """One machine as production reads it: two compose projects running one
    proxy image with advisories, an image built on the machine run by two
    containers and another by one, neither in a registry HQ can read, and one
    image pulled from a registry and run by tag alone."""

    now = timezone.now().isoformat()
    proxy = f"{PROXY}:v7.15.5@{PROXY_ID}"
    containers = [
        ("oauth2-proxy", proxy, "auth", "oauth2-proxy"),
        ("oauth2-proxy-npm", proxy, "npm", "oauth2-proxy"),
        ("hermes", f"{WEBKIT}:0.22.5", "hermes", "hermes"),
        ("inspect-converter", f"{INSPECT}:local", "inspect", "converter"),
        ("inspect-viewer", f"{INSPECT}:local", "inspect", "viewer"),
        ("kuma", f"{KUMA}:2", "kuma", "kuma"),
    ]
    store(
        "portainer.container",
        *({**running(name, reference), "stack": stack} for name, reference, stack, _ in containers),
    )
    store(
        "portainer.runtime",
        *(
            runtime(name, **KEPT, stack=stack, service=service, **(proxy_runtime if name.startswith("oauth2") else {}))
            for name, _reference, stack, service in containers
        ),
    )
    store(
        "portainer.compose_project",
        project("auth", "/opt/example/auth/compose.yaml"),
        project("npm", "/opt/example/npm/compose.yaml"),
    )
    store(
        "portainer.image",
        image(
            PROXY_ID,
            PROXY,
            "v7.15.5",
            ("oauth2-proxy", proxy, "oauth2-proxy"),
            ("oauth2-proxy-npm", proxy, "oauth2-proxy"),
        ),
        image(WEBKIT_ID, WEBKIT, "0.22.5", ("hermes", f"{WEBKIT}:0.22.5", "hermes")),
        image(
            INSPECT_ID,
            INSPECT,
            "local",
            ("inspect-converter", f"{INSPECT}:local", "converter"),
            ("inspect-viewer", f"{INSPECT}:local", "viewer"),
        ),
        image(KUMA_ID, KUMA, "2", ("kuma", f"{KUMA}:2", "kuma")),
    )
    refused = "the registry returned HTTP 401."
    store(
        "registry.image",
        {
            "image": PROXY,
            "tags": ["v7.15.5", "v7.15.6"],
            "source": "https://github.com/example/oauth2-proxy",
            "read_at": now,
        },
        {"image": f"docker.io/{WEBKIT}", "unread": f"docker.io/{WEBKIT}: {refused}", "read_at": now},
        {"image": f"docker.io/library/{INSPECT}", "unread": f"docker.io/library/{INSPECT}: {refused}", "read_at": now},
        {"image": f"docker.io/{KUMA}", "tags": ["2"], "read_at": now},
    )
    store(
        "registry.upstream",
        {
            "repository": "example/oauth2-proxy",
            "url": "https://github.com/example/oauth2-proxy",
            "read_at": now,
            "advisories": ADVISORIES,
        },
    )


def answered():
    return findings(principal=READER)["findings"]


def about_images():
    return [finding for finding in answered() if finding["rule"] in (ADVISORY, UPDATE, UNPINNED)]


def named(finding, label):
    return [fact["value"] for fact in finding["evidence"] if fact["label"] == label]


class ImageFindingTests(TestCase):
    def test_an_image_two_projects_run_is_one_advisory_naming_both_with_a_step_for_each(self):
        homelab()

        (advisory,) = [finding for finding in about_images() if finding["rule"] == ADVISORY]

        self.assertEqual(sorted(named(advisory, "Container")), ["oauth2-proxy", "oauth2-proxy-npm"])
        commands = [step["command"] for step in advisory["operator_steps"]]
        self.assertEqual(len(commands), 2, advisory["operator_steps"])
        self.assertTrue(any("/opt/example/auth/compose.yaml" in command for command in commands), commands)
        self.assertTrue(any("/opt/example/npm/compose.yaml" in command for command in commands), commands)

    def test_an_image_no_registry_answers_for_is_never_asked_to_be_pinned(self):
        homelab()

        pinned = [finding for finding in about_images() if finding["rule"] == UNPINNED]

        self.assertEqual([named(finding, "Container") for finding in pinned], [["kuma"]])
        (pin,) = named(pinned[0], "Pin it as")
        self.assertEqual(pin, f"{KUMA}:2@{KUMA_ID}")

    def test_each_finding_has_an_id_of_its_own(self):
        homelab(
            privileged=True,
            mounts=[{"type": "bind", "source": "/var/run/docker.sock", "destination": "/var/run/docker.sock"}],
        )

        ids = Counter(finding["id"] for finding in answered())

        self.assertEqual([found for found, count in ids.items() if count > 1], [])

    def test_a_container_failing_two_checks_is_one_finding(self):
        homelab(
            privileged=True,
            mounts=[{"type": "bind", "source": "/var/run/docker.sock", "destination": "/var/run/docker.sock"}],
        )

        hardening = [finding for finding in answered() if finding["rule"] == HARDENING]

        self.assertEqual(len(hardening), 2, [finding["title"] for finding in hardening])


class IdTests(SimpleTestCase):
    def test_two_findings_of_one_rule_about_two_parts_of_one_subject_have_two_ids(self):
        def about(part):
            return Finding(
                rule=UNPINNED,
                subject="machine:example-box",
                title=part,
                severity="attention",
                explanation="",
                part=part,
            )

        self.assertNotEqual(finding_facts(about("one"))["id"], finding_facts(about("two"))["id"])
        self.assertEqual(finding_facts(about("one"))["id"], finding_facts(about("one"))["id"])

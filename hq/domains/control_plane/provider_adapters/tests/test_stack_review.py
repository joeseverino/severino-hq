"""A container stack waits for a person, who is told what it reaches."""

from __future__ import annotations

from types import SimpleNamespace

from django.test import SimpleTestCase

from hq.platform.application.approvals import _warnings
from hq.domains.control_plane.provider_adapters.portainer import STACK, _stack_warnings

DANGEROUS = """services:
  agent:
    image: example/agent:1
    privileged: true
    network_mode: host
    pid: "host"
    cap_add: [NET_ADMIN]
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /:/host
"""

HARMLESS = """services:
  web:
    image: example/web:1
    volumes:
      - ./data:/data
"""


class StackReviewTests(SimpleTestCase):
    def test_a_stack_change_is_held_for_a_person(self):
        self.assertTrue(STACK.requires_approval)

    def test_everything_root_equivalent_in_a_compose_file_is_named(self):
        found = _stack_warnings({"compose": DANGEROUS, "host": "edge-1"})

        for what in ("privileged", "Docker socket", "host's network", "every process",
                     "kernel capabilities", "root filesystem"):
            with self.subTest(what=what):
                self.assertTrue(any(what in line and "edge-1" in line for line in found), found)

    def test_an_ordinary_stack_raises_nothing(self):
        self.assertEqual(_stack_warnings({"compose": HARMLESS, "host": "edge-1"}), ())

    def test_the_approval_card_carries_the_warnings_of_what_would_be_written(self):
        held = SimpleNamespace(
            resource_kind="portainer.stack",
            payload={"spec": {"compose": DANGEROUS, "host": "edge-1"}},
        )

        self.assertTrue(_warnings(held))
        self.assertEqual(_warnings(SimpleNamespace(resource_kind="portainer.stack", payload={})), ())

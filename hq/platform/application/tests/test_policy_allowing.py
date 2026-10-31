import json

from django.test import SimpleTestCase

from hq.platform.application.tailnet import policy_allowing


def allowing(policy: dict, port: int = 443) -> tuple[dict | None, str]:
    """The amended policy, parsed, and the sentence saying what moved."""

    document, summary = policy_allowing(json.dumps(policy), source="tag:watcher", target="tag:web", port=port)
    return (json.loads(document) if document else None), summary


class GrantTests(SimpleTestCase):
    def test_a_path_no_grant_names_gets_a_grant_of_its_own(self):
        policy, summary = allowing({"grants": [{"src": ["tag:other"], "dst": ["tag:web"], "ip": ["tcp:443"]}]})

        self.assertEqual(policy["grants"][-1], {"src": ["tag:watcher"], "dst": ["tag:web"], "ip": ["tcp:443"]})
        self.assertEqual(len(policy["grants"]), 2)
        self.assertIn("a grant from tag:watcher to tag:web", summary)

    def test_a_grant_between_the_two_has_the_port_appended(self):
        policy, summary = allowing({"grants": [{"src": ["tag:watcher"], "dst": ["tag:web"], "ip": ["tcp:22"]}]})

        self.assertEqual(policy["grants"], [{"src": ["tag:watcher"], "dst": ["tag:web"], "ip": ["tcp:22", "tcp:443"]}])
        self.assertIn("added to the grant", summary)

    def test_a_path_already_granted_proposes_nothing(self):
        self.assertEqual(
            allowing({"grants": [{"src": ["tag:watcher"], "dst": ["tag:web"], "ip": ["tcp:443"]}]}),
            (None, ""),
        )


class PolicyTestTests(SimpleTestCase):
    def test_a_test_denying_the_path_now_accepts_it(self):
        policy, summary = allowing(
            {"tests": [{"src": "tag:watcher", "deny": ["tag:web:443", "tag:db:5432"], "accept": ["tag:web:80"]}]}
        )

        self.assertEqual(
            policy["tests"],
            [{"src": "tag:watcher", "deny": ["tag:db:5432"], "accept": ["tag:web:80", "tag:web:443"]}],
        )
        self.assertIn("tag:web:443 moved from denied to accepted", summary)

    def test_a_test_left_denying_nothing_loses_its_deny_list(self):
        policy, _ = allowing({"tests": [{"src": "tag:watcher", "deny": ["tag:web:443"], "accept": ["tag:web:443"]}]})

        self.assertEqual(policy["tests"], [{"src": "tag:watcher", "accept": ["tag:web:443"]}])

    def test_another_source_or_protocol_and_an_unrelated_denial_stay(self):
        tests = [
            {"src": "tag:other", "deny": ["tag:web:443"]},
            {"src": "tag:watcher", "proto": "udp", "deny": ["tag:web:443"]},
            {"src": "tag:watcher", "deny": ["tag:web:22"]},
        ]

        policy, summary = allowing({"tests": tests})

        self.assertEqual(policy["tests"], tests)
        self.assertNotIn("moved", summary)

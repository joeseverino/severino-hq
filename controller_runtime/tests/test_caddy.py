"""Tests for Caddy: sweeping routes, rendering them, and finding the proxy by role."""

from __future__ import annotations

import json
from unittest import TestCase, mock

from control_plane.provider_adapters import caddy

from .. import providers
from controller_runtime import commands, connection_env
from control_plane.provider_adapters.contracts import ProviderError


ADAPTED_CADDY = {
    "apps": {
        "http": {
            "servers": {
                "srv0": {
                    "listen": [":443"],
                    "routes": [
                        {
                            "match": [{"host": ["status.example.com"]}],
                            "handle": [
                                {
                                    "handler": "subroute",
                                    "routes": [
                                        {
                                            "handle": [
                                                {
                                                    "handler": "reverse_proxy",
                                                    "upstreams": [
                                                        {"dial": "uptime-kuma:3001"}
                                                    ],
                                                }
                                            ]
                                        }
                                    ],
                                }
                            ],
                            "terminal": True,
                        },
                        {
                            "match": [{"host": ["health.example.com"]}],
                            "handle": [
                                {
                                    "handler": "subroute",
                                    "routes": [
                                        {
                                            "handle": [
                                                {
                                                    "handler": "static_response",
                                                    "body": "ok",
                                                }
                                            ]
                                        }
                                    ],
                                }
                            ],
                            "terminal": True,
                        },
                    ],
                }
            }
        }
    }
}


class CaddyRouteSweepTests(TestCase):
    """What the edge serves, read out of the config Caddy itself runs on.

    The names here answer over TLS from a box HQ sweeps, holds a credential for
    and installs the certificate on, and every one of their service pages
    read "nothing supplies this", because the only ingress HQ could describe was
    a proxy this box does not run.
    """

    def _routes(self, config):
        return {
            record["domain"]: record["upstream"]
            for record in caddy.routes(config, "an-edge")
        }

    def test_a_proxied_name_carries_the_container_it_hands_off_to(self):
        self.assertEqual(
            self._routes(ADAPTED_CADDY)["status.example.com"], "uptime-kuma:3001"
        )

    def test_a_name_caddy_answers_itself_has_no_upstream(self):
        """A redirect or a static response forwards nowhere, and that is a fact."""

        self.assertEqual(self._routes(ADAPTED_CADDY)["health.example.com"], "")

    def test_the_upstream_is_found_however_deeply_it_is_nested(self):
        """Caddy nests by how the Caddyfile was written, not by a fixed depth."""

        deep = {
            "apps": {
                "http": {
                    "servers": {
                        "srv0": {
                            "routes": [
                                {
                                    "match": [{"host": ["deep.example.com"]}],
                                    "handle": [
                                        {
                                            "handler": "subroute",
                                            "routes": [
                                                {
                                                    "handle": [
                                                        {
                                                            "handler": "subroute",
                                                            "routes": [
                                                                {
                                                                    "handle": [
                                                                        {
                                                                            "handler": "reverse_proxy",
                                                                            "upstreams": [
                                                                                {
                                                                                    "dial": "app:8080"
                                                                                }
                                                                            ],
                                                                        }
                                                                    ]
                                                                }
                                                            ],
                                                        }
                                                    ]
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                }
            }
        }

        self.assertEqual(self._routes(deep)["deep.example.com"], "app:8080")

    def test_a_route_matching_no_host_is_not_a_hostname(self):
        """Caddy's catch-all answers for anything, which is not a name."""

        catch_all = {
            "apps": {
                "http": {
                    "servers": {
                        "srv0": {
                            "routes": [
                                {
                                    "handle": [
                                        {
                                            "handler": "reverse_proxy",
                                            "upstreams": [{"dial": "app:8080"}],
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                }
            }
        }

        self.assertEqual(caddy.routes(catch_all, "an-edge"), [])

    def test_one_route_serving_several_names_becomes_one_record_each(self):
        """The rest of HQ joins on a hostname; a row holding three joins to none."""

        shared = {
            "apps": {
                "http": {
                    "servers": {
                        "srv0": {
                            "routes": [
                                {
                                    "match": [
                                        {
                                            "host": [
                                                "one.example.com",
                                                "two.example.com",
                                            ]
                                        }
                                    ],
                                    "handle": [
                                        {
                                            "handler": "reverse_proxy",
                                            "upstreams": [{"dial": "app:8080"}],
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                }
            }
        }

        self.assertEqual(
            self._routes(shared),
            {"one.example.com": "app:8080", "two.example.com": "app:8080"},
        )

    def test_a_balanced_route_names_no_single_upstream(self):
        """Two backends is not one answer, and a guess reads as a fifth fact."""

        balanced = {
            "apps": {
                "http": {
                    "servers": {
                        "srv0": {
                            "routes": [
                                {
                                    "match": [{"host": ["ha.example.com"]}],
                                    "handle": [
                                        {
                                            "handler": "reverse_proxy",
                                            "upstreams": [
                                                {"dial": "a:8080"},
                                                {"dial": "b:8080"},
                                            ],
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                }
            }
        }

        self.assertEqual(self._routes(balanced)["ha.example.com"], "")

    def test_an_edge_that_refuses_the_operation_is_skipped_not_fatal(self):
        """Most SSH hosts are not an edge, and one down host is not a blackout."""

        with (
            mock.patch.object(
                connection_env, "ssh_connection_refs", return_value=("a-box", "an-edge")
            ),
            mock.patch.object(
                commands,
                "run_ssh",
                side_effect=[
                    ProviderError("denied"),
                    json.dumps(ADAPTED_CADDY).encode(),
                ],
            ),
        ):
            found = providers.PROVIDER_INVENTORY["caddy.route"]()

        self.assertEqual(
            sorted(record["domain"] for record in found),
            ["health.example.com", "status.example.com"],
        )


class CaddyRouteRenderingTests(TestCase):
    """The file HQ writes for an edge, in the shape that edge already uses."""

    def _rendered(self, specs, certificate_directory="/opt/apps/caddy/certs"):
        return caddy.render_routes(specs, certificate_directory)

    def test_a_route_becomes_a_site_block_that_serves_its_own_tls(self):
        """Written out rather than importing the operator's snippet.

        A file that imports a name from a file HQ does not own breaks the moment
        that file is edited, which is the coupling the separate file exists to
        avoid.
        """

        out = self._rendered([{"domain": "a.example.com", "upstream": "app:8080"}])

        self.assertIn("a.example.com {", out)
        self.assertIn("\ttls /opt/apps/caddy/certs/fullchain.pem", out)
        self.assertIn("\treverse_proxy app:8080", out)

    def test_the_same_declarations_render_the_same_bytes(self):
        """A file whose order follows a query looks changed on every pass."""

        specs = [
            {"domain": "b.example.com", "upstream": "b:1"},
            {"domain": "a.example.com", "upstream": "a:1"},
        ]

        self.assertEqual(self._rendered(specs), self._rendered(list(reversed(specs))))

    def test_a_route_with_nowhere_to_forward_is_left_out(self):
        """Caddy answers some names itself. A block with no upstream is invalid."""

        out = self._rendered(
            [
                {"domain": "a.example.com", "upstream": "app:8080"},
                {"domain": "answered-here.example.com", "upstream": ""},
            ]
        )

        self.assertNotIn("answered-here", out)

    def test_it_says_whose_file_it_is(self):
        """It replaces this file wholesale, so it has to say so in the file."""

        out = self._rendered([{"domain": "a.example.com", "upstream": "app:8080"}])

        self.assertIn("Written by Severino HQ", out)

    @mock.patch("controller_runtime.commands.run_ssh")
    def test_plan_mode_is_a_complete_result_and_writes_nothing(self, ssh):
        result = providers.execute(
            {
                "kind": "caddy.route",
                "spec": {
                    "connection_ref": "edge",
                    "certificate_directory": "/certs",
                    "routes": [{"domain": "a.example.com", "upstream": "app:80"}],
                },
            },
            "reconcile",
            apply=False,
        )

        self.assertFalse(result.changed)
        self.assertEqual(result.conditions, [])
        self.assertEqual(result.status, {"routes": 1})
        ssh.assert_not_called()

    @mock.patch.dict(
        "os.environ",
        {"EDGE_CONNECTION_REF": "edge", "EDGE_MANAGES": "1"},
        clear=True,
    )
    @mock.patch("controller_runtime.commands.run_ssh")
    def test_apply_writes_the_complete_owned_file(self, ssh):
        result = providers.execute(
            {
                "kind": "caddy.route",
                "spec": {
                    "connection_ref": "edge",
                    "certificate_directory": "/certs",
                    "routes": [{"domain": "a.example.com", "upstream": "app:80"}],
                },
            },
            "reconcile",
        )

        self.assertTrue(result.changed)
        ssh.assert_called_once()
        self.assertEqual(ssh.call_args.args[:2], ("edge", "routes:write"))
        self.assertIn(b"reverse_proxy app:80", ssh.call_args.args[2])


class CaddyDiscoveryByRoleTests(TestCase):
    """Which SSH connections get asked for Caddy routes, and which never do."""

    class _Runtime:
        def __init__(self, refs, roles):
            self._refs = refs
            self._roles = roles
            self.asked = []

        def ssh_connection_refs(self):
            return self._refs

        def connection_refs_for_role(self, role):
            return tuple(r for r in self._refs if self._roles.get(r) == role)

        def ssh(self, connection_ref, operation, payload=None):
            self.asked.append((connection_ref, operation))
            return b'{"apps":{}}'

    def test_every_declared_caddy_host_is_asked(self):
        """A set, not a host. A second edge is a field, not a code change."""

        from control_plane.provider_adapters import caddy

        runtime = self._Runtime(
            ("edge", "edge-two", "shared-hosting"),
            {"edge": "caddy", "edge-two": "caddy", "shared-hosting": "cpanel"},
        )

        caddy.inventory(runtime)

        self.assertEqual(
            [ref for ref, _ in runtime.asked], ["edge", "edge-two"]
        )

    def test_a_connection_declared_for_something_else_is_never_asked(self):
        """The failure this removes: shared hosting has no Caddy and no routes.

        Asked anyway it refuses, every sweep, forever: a cost paid on a
        machine somebody else runs.
        """

        from control_plane.provider_adapters import caddy

        runtime = self._Runtime(
            ("edge", "shared-hosting"), {"edge": "caddy", "shared-hosting": "cpanel"}
        )

        caddy.inventory(runtime)

        self.assertNotIn("shared-hosting", [ref for ref, _ in runtime.asked])

    def test_nothing_declared_still_discovers(self):
        """Absence of a role is nobody having said, not nobody qualifying."""

        from control_plane.provider_adapters import caddy

        runtime = self._Runtime(("edge", "other"), {})

        caddy.inventory(runtime)

        self.assertEqual([ref for ref, _ in runtime.asked], ["edge", "other"])

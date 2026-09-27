"""Portainer readings: every record from example payloads, kept only as its schema allows."""

from __future__ import annotations

import urllib.error
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from control_plane.observations import OBSERVATIONS
from control_plane.provider_adapters.contracts import (
    CREDENTIAL_REFUSAL,
    PERMISSION_REFUSAL,
    ProviderError,
)
from control_plane.reading_parts import WHOLE, clean_refused_parts, refused_parts
from controller_runtime import providers

from . import portainer_readings as portainer
from .parts import part_ledger

ENDPOINTS = [
    {
        "Id": 1,
        "Name": "local",
        "URL": "unix:///var/run/docker.sock",
        "Type": 1,
        "Status": 1,
        "Snapshots": [
            {"DockerVersion": "27.1.1", "Time": 1700000000, "ContainerCount": 3,
             "RunningContainerCount": 2}
        ],
        "EdgeKey": "never stored",
    },
    {
        "Id": 2,
        "Name": "edge-1",
        "URL": "tcp://198.51.100.20:9001",
        "Type": 2,
        "Status": 2,
        "Agent": {"Version": "2.21.0"},
    },
]

CONTAINERS = [
    {
        "Names": ["/web"],
        "Image": "example/web:1",
        "ImageID": "sha256:" + "a" * 64,
        "Labels": {
            "com.docker.compose.project": "shop",
            "com.docker.compose.project.working_dir": "/opt/apps/shop",
            "com.docker.compose.project.config_files": "/opt/apps/shop/compose.yml",
            "com.docker.compose.service": "web",
            "SECRET_LABEL": "never stored",
        },
        "NetworkSettings": {"Networks": {"shop_default": {}, "bridge": {}}},
        "Mounts": [
            {"Type": "volume", "Name": "shop_data", "Destination": "/data", "RW": True},
            {"Type": "bind", "Source": "/opt/apps/shop/config", "Destination": "/config",
             "RW": False},
        ],
    },
    {
        "Names": ["/db"],
        "Image": "example/db",
        "ImageID": "sha256:" + "b" * 64,
        "Labels": {"com.docker.compose.project": "shop", "com.docker.compose.service": "db"},
        "NetworkSettings": {"Networks": {"shop_default": {}}},
        "Mounts": [],
    },
    {"Names": ["/controller-run"], "Labels": {"run": "this"}, "ImageID": "sha256:c"},
]

NETWORKS = [
    {"Name": "shop_default", "Id": "n1", "Driver": "bridge", "Scope": "local",
     "IPAM": {"Config": [{"Subnet": "172.18.0.0/16"}]}, "Options": {"secret": "x"}},
    {"Name": "bridge", "Id": "n0", "Driver": "bridge", "Scope": "local"},
]

VOLUMES = {
    "Volumes": [
        {"Name": "shop_data", "Driver": "local", "Mountpoint": "/var/lib/docker/volumes/shop_data",
         "Labels": {"com.docker.compose.project": "shop"}, "Options": {"password": "x"}},
        {"Name": "orphan", "Driver": "local", "Mountpoint": "/var/lib/docker/volumes/orphan"},
    ]
}

IMAGES = [
    {"Id": "sha256:" + "a" * 64, "RepoTags": ["example/web:1"],
     "RepoDigests": ["example/web@sha256:" + "d" * 64], "Created": 1700000000, "Size": 1000},
    {"Id": "sha256:" + "b" * 64, "RepoTags": ["<none>:<none>"], "RepoDigests": [],
     "Created": 1690000000},
    {"Id": "sha256:" + "e" * 64, "RepoTags": ["example/db:latest"], "Created": 1710000000},
]

STACKS = [
    {"Id": 5, "Name": "shop", "EndpointId": 1, "Status": 1, "EntryPoint": "compose.yml",
     "Env": [{"name": "TOKEN", "value": "never stored"}]},
    {"Id": 6, "Name": "elsewhere", "EndpointId": 2, "Status": 2},
]

DOCKER = {
    "/containers/json?all=1": CONTAINERS,
    "/networks": NETWORKS,
    "/volumes": VOLUMES,
    "/images/json": IMAGES,
}


def api(*, docker=None, refs=("example-portainer",)):
    environments = [
        portainer.environment(ENDPOINTS[0], "", "192.0.2.10"),
        portainer.environment(ENDPOINTS[1], "198.51.100.20", "192.0.2.10"),
    ]
    return portainer.PortainerReads(
        refs=lambda: refs,
        environments=lambda ref: environments,
        docker=docker or (lambda ref, environment_id, path: DOCKER[path]),
        stacks=lambda ref: STACKS,
        local_host=lambda: "lab-1",
        own_run=lambda container: (container.get("Labels") or {}).get("run") == "this",
    )


def read_with_parts(kind, reads=None):
    """The kept records and the parts refused, as the stored snapshot says them."""

    with mock.patch.object(portainer.PortainerReads, "through", return_value=reads or api()):
        with part_ledger() as ledger:
            records = portainer.READINGS[kind](object())
    kept, refused = OBSERVATIONS[kind].clean(records)
    assert refused == 0, records
    snapshot = SimpleNamespace(
        kind=kind, reachable=True, refused_parts=clean_refused_parts(kind, ledger)
    )
    return kept, refused_parts(snapshot)


def read(kind, reads=None):
    return read_with_parts(kind, reads)[0]


class EnvironmentTests(SimpleTestCase):
    def test_each_environment_is_a_machine_with_its_agent_and_docker(self):
        local, agent = read("portainer.environment")

        self.assertEqual(
            (local["host"], local["address"], local["local"], local["type"], local["status"]),
            ("lab-1", "192.0.2.10", True, "docker", "up"),
        )
        self.assertEqual((local["docker_version"], local["containers_running"]), ("27.1.1", 2))
        self.assertTrue(local["snapshot_at"].startswith("2023-11-14"))
        self.assertEqual(
            (agent["host"], agent["type"], agent["status"], agent["agent_version"]),
            ("edge-1", "agent", "down", "2.21.0"),
        )
        self.assertNotIn("EdgeKey", str(local))

    def test_every_record_names_its_connection(self):
        for kind in ("portainer.environment", "portainer.network", "portainer.image"):
            with self.subTest(kind=kind):
                self.assertTrue(
                    all(item["connection_ref"] == "example-portainer" for item in read(kind))
                )


class DockerReadingTests(SimpleTestCase):
    def test_a_network_lists_the_containers_on_it_and_only_reachable_environments(self):
        networks = {item["name"]: item for item in read("portainer.network")}

        self.assertEqual(networks["shop_default"]["containers"], ["db", "web"])
        self.assertEqual(networks["shop_default"]["subnets"], ["172.18.0.0/16"])
        self.assertEqual(networks["bridge"]["containers"], ["web"])
        self.assertEqual({item["host"] for item in networks.values()}, {"lab-1"})
        self.assertNotIn("Options", str(networks))

    def test_named_volumes_and_host_paths_say_who_uses_them(self):
        found = {(item["type"], item.get("name") or item["source"]): item
                 for item in read("portainer.volume")}

        data = found[("volume", "shop_data")]
        self.assertEqual(data["stack"], "shop")
        self.assertEqual(data["used_by"], [{"container": "web", "destination": "/data",
                                             "read_only": False}])
        self.assertEqual(found[("volume", "orphan")].get("used_by", []), [])
        bind = found[("bind", "/opt/apps/shop/config")]
        self.assertEqual(bind["used_by"][0]["read_only"], True)
        self.assertNotIn("password", str(found))

    def test_an_image_names_the_containers_running_it_and_drops_placeholder_tags(self):
        images = {item["id"][7:8]: item for item in read("portainer.image")}

        self.assertEqual(images["a"]["tags"], ["example/web:1"])
        self.assertEqual(
            images["a"]["containers"],
            [{"container": "web", "reference": "example/web:1", "service": "web"}],
        )
        self.assertEqual(images["b"]["tags"], [])
        self.assertEqual(images["b"]["containers"][0]["reference"], "example/db")
        self.assertNotIn("controller-run", str(images))

    def test_compose_projects_merge_labels_with_portainer_stacks(self):
        (project,) = read("portainer.compose_project")

        self.assertEqual(
            (project["name"], project["source"], project["status"], project["working_dir"]),
            ("shop", "portainer", "active", "/opt/apps/shop"),
        )
        self.assertEqual(project["containers"], ["db", "web"])
        self.assertEqual(project["config_files"], ["/opt/apps/shop/compose.yml"])
        self.assertNotIn("never stored", str(project))


def _refusal(code):
    try:
        raise ProviderError("failed") from urllib.error.HTTPError(
            "https://portainer.example.test", code, "refused", {}, None
        )
    except ProviderError as exc:
        return exc


def _http(code):
    def call(*_args):
        raise _refusal(code)

    return call


class RefusalTests(SimpleTestCase):
    def test_every_environment_refusing_is_a_refused_read_naming_the_permission(self):
        with self.assertRaises(ProviderError) as raised:
            read("portainer.network", api(docker=_http(403)))

        self.assertEqual(raised.exception.refusal, PERMISSION_REFUSAL)
        self.assertIn("environment access", str(raised.exception))

    def test_a_refused_credential_is_named_as_one(self):
        reads = api()
        reads = portainer.PortainerReads(**{**reads.__dict__, "environments": _http(401)})

        with self.assertRaises(ProviderError) as raised:
            read("portainer.environment", reads)

        self.assertEqual(raised.exception.refusal, CREDENTIAL_REFUSAL)

    def _one_failing(self, error):
        environments = [
            portainer.environment({**ENDPOINTS[0], "Id": 1}, "", "192.0.2.10"),
            portainer.environment({**ENDPOINTS[0], "Id": 3, "Name": "edge-2"},
                                  "198.51.100.30", "192.0.2.10"),
        ]

        def docker(ref, environment_id, path):
            if environment_id == 3:
                raise error
            return DOCKER[path]

        reads = portainer.PortainerReads(**{**api().__dict__, "environments": lambda ref: environments,
                                             "docker": docker})
        return read_with_parts("portainer.image", reads)

    def test_one_environment_failing_is_the_reading_refused_on_that_machine(self):
        found, (refused,) = self._one_failing(ProviderError("timed out"))

        self.assertTrue(any(item.get("id") for item in found))
        self.assertEqual({item["host"] for item in found}, {"lab-1"})
        self.assertNotIn("unread", str(found))
        self.assertEqual(
            (refused.part.name, refused.scope, refused.address, refused.connection_ref),
            (WHOLE, "edge-2", "198.51.100.30", "example-portainer"),
        )
        self.assertIn("timed out", refused.phrase)
        self.assertTrue(refused.covers("edge-2"))
        self.assertFalse(refused.covers("lab-1"))
        self.assertTrue(refused.holds({"198.51.100.30"}))
        self.assertFalse(refused.holds({"192.0.2.10"}))

    def test_one_environment_refusing_names_the_missing_permission(self):
        found, (refused,) = self._one_failing(_refusal(403))

        self.assertEqual({item["host"] for item in found}, {"lab-1"})
        self.assertEqual(refused.refusal, PERMISSION_REFUSAL)
        self.assertEqual(refused.missing, ("environment access",))
        self.assertEqual(refused.phrase, "Docker image not read: missing environment access")

    def test_every_environment_reading_refuses_no_part(self):
        found, refused = read_with_parts("portainer.image")

        self.assertTrue(found)
        self.assertEqual(refused, ())


class WiringTests(SimpleTestCase):
    """The registered readers reach Portainer through the controller's own calls."""

    def test_the_portainer_adapter_declares_every_portainer_reading(self):
        from control_plane.providers import CONTROLLER_PROVIDER_ADAPTERS

        (adapter,) = [a for a in CONTROLLER_PROVIDER_ADAPTERS if "portainer" in a.reads_through]
        self.assertEqual(dict(adapter.readings), portainer.READINGS)
        self.assertEqual(
            sorted(adapter.readings),
            sorted(kind for kind, spec in OBSERVATIONS.items() if spec.provider == "portainer"),
        )

    @mock.patch.dict(
        "os.environ",
        {
            "PORTAINER_URL": "https://portainer.example.test",
            "PORTAINER_API_TOKEN": "secret",
            "PORTAINER_CONNECTION_REF": "example-portainer",
        },
        clear=True,
    )
    @mock.patch("controller_runtime.providers.controller_id", return_value="lab-1")
    @mock.patch("controller_runtime.providers._request")
    def test_one_sweep_lists_each_environments_containers_once(self, request, _controller):
        def answer(url, **_kwargs):
            if url.endswith("/endpoints"):
                return ENDPOINTS
            if url.endswith("/stacks"):
                return STACKS
            return DOCKER[url.split("/docker", 1)[1]]

        request.side_effect = answer
        with providers.provider_snapshot():
            for kind in ("portainer.network", "portainer.volume", "portainer.image",
                         "portainer.compose_project", "portainer.environment"):
                self.assertTrue(providers.OBSERVATION_READERS[kind]())
            containers = providers.list_portainer_containers()

        listed = [call.args[0] for call in request.call_args_list if "containers/json" in call.args[0]]
        self.assertEqual(len(listed), 1)
        self.assertEqual({item["name"] for item in containers}, {"web", "db", "controller-run"})
        self.assertEqual(request.call_args.kwargs["headers"], {"X-API-Key": "secret"})

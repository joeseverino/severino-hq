"""A new host domain is its declaration plus its own app, and nothing else.

``fixture_domain`` is an app no other file names. Declaring it here, as one
more ``DomainDescriptor``, must install it, mount its URLs, put it on the bar,
register its resource, derive its create, update and delete commands and their
permissions, delete through them and count it on the health reading.
"""

from __future__ import annotations

import importlib
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TransactionTestCase, modify_settings, override_settings
from django.urls import clear_url_caches, reverse

from hq.config import urls

from .. import domains
from ..capabilities import execute_capability
from ..domains import DomainDescriptor, Mount, Records, domain_navigation, load
from ..integrations import clear_integration_graph_cache, integration_graph
from ..plugins import NavigationItem, PluginIntegration
from ..records import counts
from ..security import mcp_principal, web_principal

APP = "hq.platform.application.tests.fixture_domain"

WIDGETS = DomainDescriptor(
    id="test.widgets",
    label="Widgets",
    navigation=(NavigationItem("Widgets", "widgets:list", "widgets", 150, "Business"),),
    integration=PluginIntegration(resources=lambda: load(f"{APP}.specs:resources")()),
    apps=(APP,),
    mounts=(Mount("widgets/", f"{APP}.urls"),),
    records=Records(
        noun="widget",
        resource="widgets",
        model=f"{APP}.models:Widget",
        target="slug",
        lookup="slug",
        command=f"{APP}.service:WidgetCommand",
        save=f"{APP}.service:save_widget",
    ),
)


def _forget() -> None:
    domains.host_domains.cache_clear()
    clear_integration_graph_cache()
    importlib.reload(urls)
    clear_url_caches()


class NewDomainDeclarationTests(TransactionTestCase):
    def setUp(self):
        declared = patch.object(domains, "HOST_DOMAINS", (*domains.HOST_DOMAINS, WIDGETS))
        declared.start()
        self.addCleanup(declared.stop)
        self.addCleanup(_forget)
        # What config/settings.py computes, now that the registry has one more.
        installed = modify_settings(INSTALLED_APPS={"append": domains.host_apps()})
        installed.enable()
        self.addCleanup(installed.disable)
        _forget()
        self.model = apps.get_model("fixture_domain", "Widget")
        with connection.schema_editor() as editor:
            editor.create_model(self.model)
        self.addCleanup(self._drop)
        self.user = get_user_model().objects.create_user("operator")

    def _drop(self):
        with connection.schema_editor() as editor:
            editor.delete_model(self.model)

    def test_its_app_is_installed_and_its_urls_are_mounted(self):
        self.assertTrue(apps.is_installed(APP))
        self.assertEqual(reverse("widgets:list"), "/widgets/")
        self.client.force_login(self.user)
        self.assertEqual(self.client.get("/widgets/").content, b"widgets")

    def test_it_is_on_the_bar(self):
        self.assertIn("widgets:list", [item.route for item in domain_navigation()])

    def test_its_resource_and_commands_are_registered(self):
        graph = integration_graph()
        self.assertIn("widgets", graph.resources)
        self.assertLessEqual(
            {"widget.create", "widget.update", "widget.delete"}, set(graph.capabilities)
        )
        self.assertEqual(graph.capabilities["widget.delete"].target_label, "Widget slug")

    def test_its_permissions_are_granted_where_record_permissions_are(self):
        self.assertTrue(web_principal(self.user).permits("write_widgets", "delete_widgets"))
        self.assertFalse(mcp_principal().permits("write_widgets"))
        with override_settings(SEVERINO_MCP_ENABLE_WRITES=True, SEVERINO_MCP_ENABLE_DELETES=True):
            self.assertTrue(mcp_principal().permits("write_widgets", "delete_widgets"))

    def test_it_is_created_counted_and_deleted_through_its_commands(self):
        operator = web_principal(self.user)

        created = execute_capability(
            "widget.create", {"slug": "first", "name": "First"}, principal=operator
        )
        self.assertTrue(created["ok"], created)
        self.assertEqual(counts()["widgets"], 1)

        deleted = execute_capability(
            "widget.delete", {"confirm": "first"}, principal=operator, target="first"
        )
        self.assertEqual(deleted["deleted"]["type"], "widget", deleted)
        self.assertFalse(self.model.objects.exists())

    def test_its_commands_and_resource_are_in_the_api_document(self):
        from hq.platform.api.openapi import document

        api = document()
        for name in ("widget.create", "widget.update", "widget.delete"):
            operation = api["paths"][f"/api/v2/capabilities/{name}/"]["post"]
            self.assertEqual(operation["x-hq-capability"], name)
            self.assertEqual(operation["tags"][0], "test.widgets")
        self.assertIn("WidgetCreateRequest", api["components"]["schemas"])
        listing = api["paths"]["/api/v2/resources/widgets/"]["get"]
        self.assertEqual(listing["x-hq-resource"], "widgets")
        tags = {tag["name"]: tag for tag in api["tags"]}
        self.assertEqual(tags["test.widgets"]["parent"], "nav.business")

"""The API document says an update keeps what it is not sent: an update's
fields show no default. That an update keeps them on every door is
``test_promises_each_door.py``'s."""

from django.test import SimpleTestCase

from .. import openapi

KEPT = "A field left out keeps its value."


class TheDocumentSaysAnUpdateKeepsTests(SimpleTestCase):
    def test_an_updates_fields_show_no_default_and_its_summary_says_why(self):
        document = openapi.document()
        operation = document["paths"]["/api/v2/capabilities/project.update/"]["post"]
        body = document["components"]["schemas"]["ProjectUpdateRequest"]["properties"]["payload"]

        self.assertIn(KEPT, operation["description"] + operation.get("summary", ""))
        self.assertEqual([name for name, field in body["properties"].items() if "default" in field], [])
        self.assertNotIn("required", body)
        schemas = document["components"]["schemas"]
        created = schemas["ProjectCreateRequest"]["properties"]["payload"]
        # A command several capabilities take alike is the component named for its type.
        created = schemas[created["$ref"].rsplit("/", 1)[-1]] if "$ref" in created else created
        self.assertIn("default", created["properties"]["status"])

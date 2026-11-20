"""A field's ``Describe`` is its meaning, the help a form shows, then what
only a client reads; the form takes its help from it (command_forms)."""

from typing import Annotated

from django.test import SimpleTestCase
from pydantic import BaseModel


class CommandDescriptionTests(SimpleTestCase):
    def test_a_description_for_clients_alone_shows_no_help(self):
        from hq.platform.application.describe import Describe, meanings

        class Asked(BaseModel):
            hosts: Annotated[str, Describe("", api="Each by its name.")]

        self.assertEqual(meanings(Asked), {"hosts": ""})
        self.assertEqual(Asked.model_json_schema()["properties"]["hosts"]["description"], "Each by its name.")

    def test_a_declared_example_is_what_the_schema_offers_beside_the_description(self):
        from hq.platform.application.describe import Describe

        class Asked(BaseModel):
            about: Annotated[
                str, Describe("What it is about.", api="A `type:key` reference.", examples=("machine:lab-1",))
            ]

        said = Asked.model_json_schema()["properties"]["about"]
        self.assertEqual(said["examples"], ["machine:lab-1"])
        self.assertEqual(said["description"], "What it is about. A `type:key` reference.")

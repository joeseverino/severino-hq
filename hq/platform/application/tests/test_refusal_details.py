"""A refusal's details come in the shapes the envelope's schema names, which
both doors publish, so a client can read them by that schema."""

from typing import Any

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
from jsonschema import Draft202012Validator

from hq.domains.docs_index.importer import validate_manifest_data
from hq.platform.application.input_errors import django_refusal, pydantic_refusal
from hq.platform.application.refusal_status import Refusal, next_call
from hq.platform.mcp.shapes import REFUSED


def _meets(details: Any) -> bool:
    return Draft202012Validator(REFUSED).is_valid(Refusal("invalid_input", "Example refused.", details).answer())


class RefusalDetailsTests(SimpleTestCase):
    def test_each_shape_a_refusal_gives_its_details_meets_the_schema(self):
        shapes = {
            "pointers": {"next": next_call("describe_resources", name="example"), "field": "target"},
            "pydantic": pydantic_refusal(
                "example", [{"type": "missing", "loc": ("title",), "msg": "Required"}]
            ).details,
            "django": django_refusal(
                "example", ValidationError({"title": ValidationError("x", code="required")})
            ).details,
            "records": validate_manifest_data([{}]),
        }
        for shape, details in shapes.items():
            with self.subTest(shape):
                self.assertTrue(_meets(details))

    def test_details_in_no_shape_a_refusal_gives_do_not_meet_it(self):
        for details in ({"next": "describe_resources"}, {"scope": 3}, ["target"]):
            with self.subTest(details=details):
                self.assertFalse(_meets(details))

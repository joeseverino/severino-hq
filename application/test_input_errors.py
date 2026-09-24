from __future__ import annotations

from enum import Enum

from django.test import SimpleTestCase
from pydantic import BaseModel, ValidationError

from .input_errors import pydantic_refusal


class Size(Enum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class Item(BaseModel):
    size: Size
    quantity: int


class Order(BaseModel):
    items: list[Item]


def _message(model, payload):
    try:
        model.model_validate(payload)
    except ValidationError as exc:
        return pydantic_refusal("example.order", exc.errors()).message
    raise AssertionError("expected a refusal")


class InvalidInputMessageTests(SimpleTestCase):
    def test_a_nested_path_and_every_declared_choice_are_named(self):
        message = _message(Order, {"items": [{"size": "huge", "quantity": 1}]})

        self.assertEqual(
            message,
            "example.order: items[0].size must be one of small, medium, large.",
        )

    def test_a_long_list_of_problems_points_at_details(self):
        items = [{"size": "small"} for _ in range(7)]

        message = _message(Order, {"items": items})

        self.assertTrue(message.endswith("; and 2 more (see details)."), message)
        self.assertEqual(message.count("is required"), 5)

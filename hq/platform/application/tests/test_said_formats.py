"""A command's type says the form a value takes as the server takes it: the
schema's pattern or bounds refuse what the server refuses, and take what it takes."""

import re
from decimal import Decimal
from typing import Annotated

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import URLValidator
from django.test import SimpleTestCase, TestCase
from pydantic import Field, TypeAdapter, ValidationError

from ..json_schemas import json_schema
from ..resources import get_resource
from ..security import cli_principal

# What a URL column takes or refuses, each a case its validator decides.
URLS = (
    "https://example.com",
    "HTTP://EXAMPLE.COM/path?q=1#x",
    "ftp://user:pw@example.com:21/",
    "https://[2001:db8::1]/",
    "https://192.0.2.1:8080",
    "http://localhost",
    "http://LOCALHOST:3000",
    "https://xn--bcher-kva.example/",
    "https://bücher.example",
    "https://example.com.",
    "https://-bad.example",
    "https://bad-.example",
    "https://example.c",
    "https://example.-om",
    "mailto:someone@example.com",
    "https://exa mple.com",
    "https://example",
    "https://256.1.1.1",
    "https://example.com:123456",
)


class URLPatternTests(SimpleTestCase):
    def test_a_url_columns_pattern_takes_what_its_validator_takes(self):
        from ..stored_columns import url_pattern

        rule = URLValidator()
        pattern = url_pattern(rule.schemes)
        typed = TypeAdapter(Annotated[str, Field(pattern=pattern)])
        for url in URLS:
            with self.subTest(url=url):
                try:
                    rule(url)
                    taken = True
                except DjangoValidationError:
                    taken = False
                # The schema's reader and pydantic's each read the one pattern.
                self.assertIs(bool(re.fullmatch(pattern, url)), taken)
                try:
                    typed.validate_python(url)
                    self.assertTrue(taken)
                except ValidationError:
                    self.assertFalse(taken)


class DecimalTests(SimpleTestCase):
    def test_a_sized_decimal_says_its_digits_as_a_number_and_as_text(self):
        amount = Annotated[Decimal, Field(max_digits=4, decimal_places=2)]
        schema = json_schema(TypeAdapter(amount))
        number, text = schema["anyOf"]
        typed = TypeAdapter(amount)

        self.assertEqual((number["exclusiveMinimum"], number["exclusiveMaximum"]), (-100, 100))
        for value, taken in (("12.34", True), ("-0.5", True), ("12.340", True), ("123.4", False), ("1.234", False)):
            with self.subTest(value=value):
                self.assertIs(bool(re.fullmatch(text["pattern"], value)), taken)
                try:
                    typed.validate_python(value)
                    self.assertTrue(taken)
                except ValidationError:
                    self.assertFalse(taken)


class MissingRecordTests(TestCase):
    def test_an_identifier_its_key_cannot_hold_names_no_record(self):
        from ..record_errors import RecordNotFound

        # A calendar entry's uid is a UUID, which "next" cannot be.
        with self.assertRaises(RecordNotFound):
            get_resource("calendar", "next", principal=cli_principal())

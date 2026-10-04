"""Expense year filters cannot overflow the database date lookup."""

from django.test import TestCase, override_settings

from .testing import ContractClient
from .tests import ISSUER, RESOURCE, _serving, _token


@override_settings(OIDC_ISSUER=ISSUER, SEVERINO_API_RESOURCE=RESOURCE)
class ExpenseYearQueryTests(TestCase):
    client_class = ContractClient

    def test_supported_date_years_and_default_are_accepted(self):
        with _serving():
            token = _token(scope="read")
            for query in ({}, {"year": 1}, {"year": 9999}):
                with self.subTest(query=query):
                    response = self.client.get(
                        "/api/v2/resources/expenses/",
                        query,
                        HTTP_AUTHORIZATION=f"Bearer {token}",
                    )
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.json()["ok"])

    def test_years_outside_date_range_are_refused_before_database_lookup(self):
        with _serving():
            token = _token(scope="read")
            for year in (0, -1, 10000, -(10**30), 10**30):
                with self.subTest(year=year):
                    response = self.client.get(
                        "/api/v2/resources/expenses/",
                        {"year": year},
                        HTTP_AUTHORIZATION=f"Bearer {token}",
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json()["error"]["code"], "invalid_input")

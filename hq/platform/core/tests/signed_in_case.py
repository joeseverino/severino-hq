"""A test case signed in as a superuser, for the web shell's pages."""

from django.contrib.auth import get_user_model
from django.test import Client, TestCase

User = get_user_model()


class _AuthedTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser(
            username="tester",
            email="t@example.com",
            password="strongtestpass-1234",
        )

    def setUp(self):
        self.client = Client()
        assert self.client.login(username="tester", password="strongtestpass-1234")

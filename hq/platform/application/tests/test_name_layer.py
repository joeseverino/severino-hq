"""Whether a request's name is out of public DNS is a question only a name can answer."""

from unittest import mock

from django.test import RequestFactory, SimpleTestCase, override_settings

from ..request_layers import _name_layer


@override_settings(ALLOWED_HOSTS=["*"])
class NameLayerTests(SimpleTestCase):
    def layer(self, host):
        request = RequestFactory().get("/", HTTP_HOST=host)
        with mock.patch("hq.platform.application.request_layers.public_answers_for", return_value=()) as answers:
            return _name_layer(request), answers

    def test_a_request_by_address_is_not_judged_by_dns(self):
        for host in ("192.0.2.10:8000", "[2001:db8::1]:8000"):
            with self.subTest(host=host):
                layer, answers = self.layer(host)

                self.assertEqual(layer.state, "unknown")
                answers.assert_not_called()

    def test_a_name_with_no_public_record_holds(self):
        layer, answers = self.layer("hq.example.test:8000")

        self.assertEqual(layer.state, "holds")
        answers.assert_called_once_with("hq.example.test")

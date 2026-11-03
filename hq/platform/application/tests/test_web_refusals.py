"""A web view turns a refused answer into what a person sees one way: ``web_writes.refused``."""

from django import forms
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.backends.signed_cookies import SessionStore
from django.test import RequestFactory, SimpleTestCase

from ..refusal_status import refuse
from ..web_writes import refusal_response, refused

SENTENCE = "Example: name is required."


class ExampleForm(forms.Form):
    name = forms.CharField(required=False)


def a_request():
    request = RequestFactory().post("/example/")
    request.session = SessionStore()
    request._messages = FallbackStorage(request)
    return request


def said(request) -> list[str]:
    return [str(message) for message in get_messages(request)]


class RefusedTests(SimpleTestCase):
    def test_an_answer_that_ran_is_no_refusal_and_says_nothing(self):
        request = a_request()

        self.assertIsNone(refused(request, {"ok": True}))
        self.assertEqual(said(request), [])

    def test_without_a_form_the_refusals_own_sentence_goes_on_the_next_page(self):
        request = a_request()

        refusal = refused(request, refuse("forbidden", SENTENCE))

        self.assertEqual(refusal.status, 403)
        self.assertEqual(said(request), [SENTENCE])

    def test_a_detail_naming_a_field_goes_beside_it(self):
        form = ExampleForm({"name": ""})
        form.is_valid()
        answer = refuse("invalid_input", SENTENCE, [{"type": "missing", "loc": ["name"], "msg": "Field required"}])

        refusal = refused(a_request(), answer, form=form)

        self.assertEqual(refusal.status, 400)
        self.assertEqual(form.errors["name"], ["Field required"])
        self.assertEqual(form.non_field_errors(), [])

    def test_a_detail_naming_no_field_on_the_form_leaves_the_sentence_above_it(self):
        form = ExampleForm({"name": ""})
        form.is_valid()
        answer = refuse("invalid_input", SENTENCE, [{"type": "missing", "loc": ["elsewhere"], "msg": "Field required"}])

        refused(a_request(), answer, form=form)

        self.assertEqual(form.non_field_errors(), [SENTENCE])

    def test_a_refusal_as_the_response_carries_its_codes_status_and_its_sentence(self):
        for code, status in (("not_found", 404), ("forbidden", 403), ("invalid_input", 400), ("conflict", 409)):
            with self.subTest(code=code):
                response = refusal_response(refuse(code, SENTENCE))

                self.assertEqual(response.status_code, status)
                self.assertEqual(response.content.decode(), SENTENCE)

"""The shared pieces a write or list view is built from: file fields, rows, totals and the write spine."""

from django.test import SimpleTestCase


class MultipleFileFieldTests(SimpleTestCase):
    """Several files selected must mean several files imported.

    Django's FileField binds one upload; pointed at a multiple input it keeps
    whichever the widget returned and drops the rest silently: an operator
    would see one file land and no explanation for the others.
    """

    @staticmethod
    def _upload(name, body=b"x"):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return SimpleUploadedFile(name, body)

    def test_no_selection_raises_the_ordinary_required_error(self):
        from django.core.exceptions import ValidationError as FormValidationError

        from ..forms import MultipleFileField

        with self.assertRaises(FormValidationError):
            MultipleFileField(max_bytes=8, max_files=2).clean([])
        self.assertEqual(MultipleFileField(max_bytes=8, max_files=2, required=False).clean([]), [])

    def test_a_file_over_the_declared_size_or_count_is_refused_in_the_words_its_input_carries(self):
        from django.core.exceptions import ValidationError as FormValidationError

        from ..forms import MultipleFileField

        field = MultipleFileField(max_bytes=8, max_files=2)
        attrs = field.widget.attrs
        with self.assertRaises(FormValidationError) as refused:
            field.clean([self._upload("big.csv", b"x" * 9)])
        self.assertEqual(refused.exception.messages, [f"big.csv {attrs['data-too-large']}"])
        with self.assertRaises(FormValidationError) as refused:
            field.clean([self._upload("a.csv"), self._upload("b.csv"), self._upload("c.csv")])
        self.assertEqual(refused.exception.messages, [attrs["data-too-many"]])
        self.assertEqual(len(field.clean([self._upload("a.csv", b"x" * 8)])), 1)

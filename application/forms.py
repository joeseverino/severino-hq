"""Form pieces every HQ surface can reuse.

These live in the host because what they solve is a property of the browser and
of Django, not of any one domain. A surface that accepts several files at once
should not have to rediscover that ``FileField`` binds exactly one upload.
"""

from __future__ import annotations

from django import forms


class MultipleFileInput(forms.FileInput):
    """A file input that reports every selected file.

    Django gates multiple selection behind this flag rather than the ``multiple``
    attribute, because a widget that renders ``multiple`` while reading a single
    file is worse than one that refuses -- it silently drops uploads. Setting the
    flag makes the widget read ``files.getlist`` and add the attribute itself.
    """

    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """A ``FileField`` that validates every selected file, not just one.

    ``FileField.clean`` is written for a single upload. Pointed at a multiple
    input it keeps whichever file the widget happened to return and discards the
    rest, so an operator who selected three files would see one imported and no
    error explaining the other two. This runs the field's own validation --
    size, extension, whatever a subclass adds -- over each upload and returns
    the full list, so the caller gets either every file or the first failure.
    """

    widget = MultipleFileInput

    def clean(self, data, initial=None):
        # Bound before the comprehension: a zero-argument ``super()`` inside one
        # resolves against the comprehension's own scope, not this method's.
        clean_one = super().clean
        if isinstance(data, (list, tuple)):
            uploads = [item for item in data if item not in self.empty_values]
        else:
            uploads = [] if data in self.empty_values else [data]
        if not uploads:
            # Delegate the empty case so a required field raises the same message
            # every other field on the form would.
            clean_one(None, initial)
            return []
        return [clean_one(item, initial) for item in uploads]
